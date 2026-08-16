"""Postgres: the record of truth for an inspection in progress.

An inspection is walked over hours, on a phone, in an attic and a crawlspace, and the report
is assembled afterwards. So the state that matters — the findings, the severities, the photo
hashes and the decisions taken at the gate — lives in a database rather than in a process.

Two things this buys that memory does not:

* **A walk survives a restart.** Close the tab, drop the connection, redeploy the service, and
  the inspection is where it was.
* **Photograph uploads stay idempotent across runs.** A photo's identity is the hash of its
  cleaned bytes, so the URL it was given is remembered and never bought twice.

The schema is applied on connect and is safe to re-run.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from inspection_report.domain.models import Finding, Inspection, Inspector, Photo, Property
from inspection_report.logging import get_logger

_log = get_logger("inspection_report.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS inspections (
    id             UUID PRIMARY KEY,
    address_line   TEXT        NOT NULL,
    city           TEXT        NOT NULL,
    postcode       TEXT        NOT NULL DEFAULT '',
    year_built     INTEGER,
    property_type  TEXT        NOT NULL DEFAULT '',
    inspector_name TEXT        NOT NULL,
    licence_number TEXT        NOT NULL DEFAULT '',
    firm_name      TEXT        NOT NULL DEFAULT '',
    inspected_on   DATE        NOT NULL,
    template_key   TEXT        NOT NULL DEFAULT 'buyer_summary',
    stage          TEXT        NOT NULL DEFAULT 'draft',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS findings (
    id             UUID PRIMARY KEY,
    inspection_id  UUID        NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    system_key     TEXT        NOT NULL,
    severity_key   TEXT        NOT NULL,
    location       TEXT        NOT NULL DEFAULT '',
    observation    TEXT        NOT NULL,
    recommendation TEXT        NOT NULL DEFAULT '',
    plain_language TEXT        NOT NULL DEFAULT '',
    position       INTEGER     NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS findings_by_inspection ON findings (inspection_id, position);

CREATE TABLE IF NOT EXISTS photos (
    id            UUID PRIMARY KEY,
    finding_id    UUID        NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    sha256        TEXT        NOT NULL,
    filename      TEXT        NOT NULL,
    content_type  TEXT        NOT NULL,
    size_bytes    INTEGER     NOT NULL,
    width         INTEGER     NOT NULL DEFAULT 0,
    height        INTEGER     NOT NULL DEFAULT 0,
    caption       TEXT        NOT NULL DEFAULT '',
    position      INTEGER     NOT NULL DEFAULT 0,
    bytes         BYTEA       NOT NULL,
    thumbnail     BYTEA,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS photos_by_finding ON photos (finding_id, position);

-- The upload cache. Keyed by content, because that is what a photograph's identity is:
-- the same bytes are the same photograph however many findings or runs reference them, and
-- an upload that has been paid for once is never paid for again.
CREATE TABLE IF NOT EXISTS photo_uploads (
    sha256     TEXT PRIMARY KEY,
    remote_url TEXT        NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every proposal the AI made and what was decided about it, including the rail's verdict.
-- Kept because "why does the report say this" must be answerable after the fact.
CREATE TABLE IF NOT EXISTS proposals (
    id            UUID PRIMARY KEY,
    inspection_id UUID        NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    finding_id    UUID        REFERENCES findings(id) ON DELETE SET NULL,
    job_id        TEXT        NOT NULL DEFAULT '',
    change_id     TEXT        NOT NULL DEFAULT '',
    old_text      TEXT        NOT NULL DEFAULT '',
    new_text      TEXT        NOT NULL DEFAULT '',
    rail_clean    BOOLEAN     NOT NULL DEFAULT TRUE,
    rail_breaches JSONB       NOT NULL DEFAULT '[]'::jsonb,
    decision      TEXT        NOT NULL DEFAULT 'pending',
    decided_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS proposals_by_inspection ON proposals (inspection_id);
"""


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://inspect:inspect@db:5432/inspect")


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    with psycopg.connect(url or database_url(), row_factory=dict_row) as conn:
        yield conn


def apply_schema(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Idempotent. Safe on every start."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()


# ------------------------------------------------------------------ writes


def save_inspection(conn: psycopg.Connection[dict[str, Any]], inspection: Inspection) -> None:
    """Upsert an inspection and its findings, in one transaction.

    Findings are **upserted**, and only the ones that are genuinely gone are deleted. That
    distinction is the whole point: ``photos.finding_id`` is ``ON DELETE CASCADE``, so
    clearing the findings and re-inserting them — which is what this used to do — destroyed
    every photograph on the inspection. Re-inserting a finding with the same id does not
    bring its photographs back.

    It fired on the ordinary path: attach a photograph to a finding, record the next
    finding, and the first photograph was gone. Approving decisions and exporting did the
    same thing, because both save the inspection on their way through.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO inspections (id, address_line, city, postcode, year_built,
                property_type, inspector_name, licence_number, firm_name, inspected_on,
                template_key, stage)
            VALUES (%(id)s, %(address_line)s, %(city)s, %(postcode)s, %(year_built)s,
                %(property_type)s, %(inspector_name)s, %(licence_number)s, %(firm_name)s,
                %(inspected_on)s, %(template_key)s, %(stage)s)
            ON CONFLICT (id) DO UPDATE SET
                address_line = EXCLUDED.address_line, city = EXCLUDED.city,
                postcode = EXCLUDED.postcode, year_built = EXCLUDED.year_built,
                property_type = EXCLUDED.property_type,
                inspector_name = EXCLUDED.inspector_name,
                licence_number = EXCLUDED.licence_number, firm_name = EXCLUDED.firm_name,
                inspected_on = EXCLUDED.inspected_on, template_key = EXCLUDED.template_key,
                stage = EXCLUDED.stage, updated_at = now()
            """,
            {
                "id": inspection.id,
                "address_line": inspection.property.address_line,
                "city": inspection.property.city,
                "postcode": inspection.property.postcode,
                "year_built": inspection.property.year_built,
                "property_type": inspection.property.property_type,
                "inspector_name": inspection.inspector.name,
                "licence_number": inspection.inspector.licence_number,
                "firm_name": inspection.inspector.firm_name,
                "inspected_on": inspection.inspected_on,
                "template_key": inspection.template_key,
                "stage": str(inspection.stage),
            },
        )
        # Remove only what is no longer here. An empty list deletes every finding, which is
        # the correct reading of "this inspection now has none".
        cur.execute(
            "DELETE FROM findings WHERE inspection_id = %s AND NOT (id = ANY(%s))",
            (inspection.id, [f.id for f in inspection.findings]),
        )
        for position, finding in enumerate(inspection.findings):
            cur.execute(
                """
                INSERT INTO findings (id, inspection_id, system_key, severity_key, location,
                    observation, recommendation, plain_language, position)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    system_key = EXCLUDED.system_key,
                    severity_key = EXCLUDED.severity_key,
                    location = EXCLUDED.location,
                    observation = EXCLUDED.observation,
                    recommendation = EXCLUDED.recommendation,
                    plain_language = EXCLUDED.plain_language,
                    position = EXCLUDED.position
                """,
                (
                    finding.id,
                    inspection.id,
                    finding.system_key,
                    finding.severity_key,
                    finding.location,
                    finding.observation,
                    finding.recommendation,
                    finding.plain_language,
                    position,
                ),
            )
    conn.commit()


def save_photo(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    finding_id: UUID,
    photo: Photo,
    data: bytes,
    thumbnail: bytes | None,
    position: int = 0,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO photos (id, finding_id, sha256, filename, content_type, size_bytes,
                width, height, caption, position, bytes, thumbnail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET caption = EXCLUDED.caption,
                position = EXCLUDED.position
            """,
            (
                photo.id,
                finding_id,
                photo.sha256,
                photo.filename,
                photo.content_type,
                photo.size_bytes,
                photo.width,
                photo.height,
                photo.caption,
                position,
                data,
                thumbnail,
            ),
        )
    conn.commit()


def remember_upload(
    conn: psycopg.Connection[dict[str, Any]], *, sha256: str, remote_url: str
) -> None:
    """Record that these bytes have already been uploaded, and to where."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO photo_uploads (sha256, remote_url) VALUES (%s, %s)
            ON CONFLICT (sha256) DO NOTHING
            """,
            (sha256, remote_url),
        )
    conn.commit()


def known_uploads(conn: psycopg.Connection[dict[str, Any]]) -> dict[str, str]:
    """Every photograph already uploaded, by content hash. Feeds upload idempotency."""
    with conn.cursor() as cur:
        cur.execute("SELECT sha256, remote_url FROM photo_uploads")
        return {r["sha256"]: r["remote_url"] for r in cur.fetchall()}


# Stable namespace for deriving a proposal's primary key from (inspection, change_id).
_PROPOSAL_NS = UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def record_proposals(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    inspection_id: UUID,
    rows: list[dict[str, Any]],
) -> None:
    """Keep every proposal and the rail's verdict on it, decided or not.

    A proposal is identified by its ``change_id`` within an inspection, so the primary key is
    derived from that pair rather than minted fresh. It used to be a new ``uuid4()`` on every
    call, which meant the ``ON CONFLICT`` below never fired: recording the same round twice —
    once when the proposals arrive, once when they are decided — inserted a second set instead
    of updating the first, and left the audit trail holding a stale `pending` copy of every
    decided change. "Why does the report say this" has to have one answer.
    """
    with conn.cursor() as cur:
        for row in rows:
            row = {
                **row,
                "id": uuid5(_PROPOSAL_NS, f"{inspection_id}:{row['change_id']}"),
            }
            cur.execute(
                """
                INSERT INTO proposals (id, inspection_id, finding_id, job_id, change_id,
                    old_text, new_text, rail_clean, rail_breaches, decision, decided_at)
                VALUES (%(id)s, %(inspection_id)s, %(finding_id)s, %(job_id)s, %(change_id)s,
                    %(old_text)s, %(new_text)s, %(rail_clean)s, %(rail_breaches)s,
                    %(decision)s, %(decided_at)s)
                ON CONFLICT (id) DO UPDATE SET decision = EXCLUDED.decision,
                    decided_at = EXCLUDED.decided_at
                """,
                {
                    **row,
                    "inspection_id": inspection_id,
                    "rail_breaches": json.dumps(row.get("rail_breaches", [])),
                },
            )
    conn.commit()


# ------------------------------------------------------------------- reads


def load_inspection(
    conn: psycopg.Connection[dict[str, Any]], inspection_id: UUID
) -> Inspection | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM inspections WHERE id = %s", (inspection_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "SELECT * FROM findings WHERE inspection_id = %s ORDER BY position",
            (inspection_id,),
        )
        finding_rows = cur.fetchall()
        finding_ids = [r["id"] for r in finding_rows]
        photos_by_finding: dict[UUID, list[Photo]] = {fid: [] for fid in finding_ids}
        if finding_ids:
            cur.execute(
                "SELECT * FROM photos WHERE finding_id = ANY(%s) ORDER BY position",
                (finding_ids,),
            )
            for p in cur.fetchall():
                photos_by_finding[p["finding_id"]].append(
                    Photo(
                        id=p["id"],
                        sha256=p["sha256"],
                        filename=p["filename"],
                        content_type=p["content_type"],
                        size_bytes=p["size_bytes"],
                        width=p["width"],
                        height=p["height"],
                        caption=p["caption"],
                    )
                )

    uploads = known_uploads(conn)
    findings = []
    for r in finding_rows:
        photos = photos_by_finding.get(r["id"], [])
        for photo in photos:
            photo.remote_url = uploads.get(photo.sha256, "")
        findings.append(
            Finding(
                id=r["id"],
                system_key=r["system_key"],
                severity_key=r["severity_key"],
                location=r["location"],
                observation=r["observation"],
                recommendation=r["recommendation"],
                plain_language=r["plain_language"],
                photos=photos,
            )
        )

    return Inspection(
        id=row["id"],
        property=Property(
            address_line=row["address_line"],
            city=row["city"],
            postcode=row["postcode"],
            year_built=row["year_built"],
            property_type=row["property_type"],
        ),
        inspector=Inspector(
            name=row["inspector_name"],
            licence_number=row["licence_number"],
            firm_name=row["firm_name"],
        ),
        inspected_on=row["inspected_on"],
        template_key=row["template_key"],
        stage=row["stage"],
        findings=findings,
    )


def list_inspections(conn: psycopg.Connection[dict[str, Any]]) -> list[dict[str, Any]]:
    """The inspections list, with enough for a card without loading every photograph."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.id, i.address_line, i.city, i.postcode, i.inspector_name, i.firm_name,
                   i.inspected_on, i.stage, i.template_key, i.updated_at,
                   COUNT(f.id) AS finding_count
            FROM inspections i
            LEFT JOIN findings f ON f.inspection_id = i.id
            GROUP BY i.id
            ORDER BY i.updated_at DESC
            """
        )
        return list(cur.fetchall())


def photo_bytes(
    conn: psycopg.Connection[dict[str, Any]], photo_id: UUID, *, thumbnail: bool = False
) -> tuple[bytes, str] | None:
    column = "thumbnail" if thumbnail else "bytes"
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {column} AS data, content_type FROM photos WHERE id = %s",
            (photo_id,),
        )
        row = cur.fetchone()
    if row is None or row["data"] is None:
        return None
    mime = "image/jpeg" if thumbnail else row["content_type"]
    return bytes(row["data"]), mime


def photo_data_for(
    conn: psycopg.Connection[dict[str, Any]], inspection_id: UUID
) -> dict[str, bytes]:
    """Every photograph's cleaned bytes for one inspection, keyed by filename.

    Keyed by filename because that is what the render pipeline asks for; identity for
    upload purposes is still the content hash.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.filename, p.bytes FROM photos p
            JOIN findings f ON f.id = p.finding_id
            WHERE f.inspection_id = %s
            """,
            (inspection_id,),
        )
        return {r["filename"]: bytes(r["bytes"]) for r in cur.fetchall()}


def load_proposals(
    conn: psycopg.Connection[dict[str, Any]], inspection_id: UUID
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM proposals WHERE inspection_id = %s ORDER BY created_at",
            (inspection_id,),
        )
        return list(cur.fetchall())


def clear_proposals(conn: psycopg.Connection[dict[str, Any]], inspection_id: UUID) -> None:
    """A fresh proposal round replaces the last one; stale cards must not linger at a gate."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM proposals WHERE inspection_id = %s", (inspection_id,))
    conn.commit()
