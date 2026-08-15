"""The HTTP surface the browser talks to.

The SuperDocs key lives here and never goes further. That is not a preference — it is the
first hard constraint in SuperDocs' own integration guidance, and it is why the inspector
never sees a second product: they use this application, and SuperDocs is plumbing behind it.

Photographs are served from our own database rather than by handing the browser the URL
SuperDocs returned. That URL is readable by anyone who holds it and never expires, so putting
it in a page, a browser history or a referrer header would leak a photograph of a client's
home. The browser gets `/api/photos/{id}` instead.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from inspection_report.domain import catalogue
from inspection_report.domain.models import (
    Finding,
    Inspection,
    Inspector,
    Property,
    ReportStage,
)
from inspection_report.logging import get_logger, setup_logging
from inspection_report.photos.pipeline import PhotoRejected, clean
from inspection_report.phrasing import rail
from inspection_report.store import db
from inspection_report.templates import docx_html

_log = get_logger("inspection_report.api")

TEMPLATE_DIR = Path(os.environ.get("TEMPLATE_DIR", "templates"))
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:5174").split(",")
    if o.strip()
]

app = FastAPI(
    title="Home-inspection report builder",
    description="Findings and photo evidence per inspection system, assembled into a "
    "formatted field report. Built on SuperDocs.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,  # never "*": this serves photographs of people's homes
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    setup_logging(os.environ.get("LOG_FORMAT", "json"))
    try:
        with db.connect() as conn:
            db.apply_schema(conn)
        _log.info("schema_ready")
    except Exception as exc:  # pragma: no cover - depends on a live database
        # Degrade rather than refuse to boot: /health and the catalogue still answer, and the
        # error names the cause instead of a stack trace in a browser console.
        _log.error("database_unavailable", extra={"error": type(exc).__name__})


def get_conn() -> Any:
    try:
        with db.connect() as conn:
            yield conn
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "the database is not reachable. If you are running this outside Docker, "
                "set DATABASE_URL; inside `docker compose up` it is set for you."
            ),
        ) from exc


# ------------------------------------------------------------------ schemas


class PropertyIn(BaseModel):
    address_line: str
    city: str
    postcode: str = ""
    year_built: int | None = None
    property_type: str = ""


class InspectorIn(BaseModel):
    name: str
    licence_number: str = ""
    firm_name: str = ""


class InspectionIn(BaseModel):
    property: PropertyIn
    inspector: InspectorIn
    inspected_on: str
    template_key: str = "buyer_summary"


class FindingIn(BaseModel):
    system_key: str
    severity_key: str
    location: str = ""
    observation: str = Field(min_length=1)
    recommendation: str = ""


# ------------------------------------------------------------------- routes


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/catalogue")
def get_catalogue() -> dict[str, Any]:
    """Systems, severities and the language rail's rules — everything the UI renders from."""
    return {
        "systems": [s.model_dump() for s in catalogue.systems()],
        "severities": [s.model_dump() for s in catalogue.severities()],
        "rail_rules": rail.describe_rules(),
        "formats": sorted(p.stem for p in TEMPLATE_DIR.glob("*.docx")),
    }


@app.get("/api/inspections")
def list_inspections(conn: Any = Depends(get_conn)) -> list[dict[str, Any]]:
    return [
        {**row, "id": str(row["id"]), "inspected_on": row["inspected_on"].isoformat()}
        for row in db.list_inspections(conn)
    ]


@app.post("/api/inspections", status_code=201)
def create_inspection(body: InspectionIn, conn: Any = Depends(get_conn)) -> dict[str, str]:
    import datetime as dt

    if body.template_key not in {p.stem for p in TEMPLATE_DIR.glob("*.docx")}:
        raise HTTPException(
            status_code=400,
            detail=f"unknown report format {body.template_key!r}. "
            f"Available: {sorted(p.stem for p in TEMPLATE_DIR.glob('*.docx'))}",
        )
    inspection = Inspection(
        property=Property(**body.property.model_dump()),
        inspector=Inspector(**body.inspector.model_dump()),
        inspected_on=dt.date.fromisoformat(body.inspected_on),
        template_key=body.template_key,
    )
    db.save_inspection(conn, inspection)
    return {"id": str(inspection.id)}


@app.get("/api/inspections/{inspection_id}")
def get_inspection(inspection_id: UUID, conn: Any = Depends(get_conn)) -> dict[str, Any]:
    inspection = db.load_inspection(conn, inspection_id)
    if inspection is None:
        raise HTTPException(status_code=404, detail="no inspection with that id")
    return _serialise(inspection)


@app.post("/api/inspections/{inspection_id}/findings", status_code=201)
def add_finding(
    inspection_id: UUID, body: FindingIn, conn: Any = Depends(get_conn)
) -> dict[str, str]:
    inspection = db.load_inspection(conn, inspection_id)
    if inspection is None:
        raise HTTPException(status_code=404, detail="no inspection with that id")
    # Validate against the catalogue here, so an unknown key is a 400 naming the valid set
    # rather than a finding that renders into nothing later.
    try:
        catalogue.system(body.system_key)
        catalogue.severity(body.severity_key)
    except catalogue.CatalogueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    finding = Finding(**body.model_dump())
    inspection.findings.append(finding)
    db.save_inspection(conn, inspection)
    return {"id": str(finding.id)}


@app.post("/api/findings/{finding_id}/photos", status_code=201)
async def add_photo(
    finding_id: UUID,
    file: UploadFile = File(...),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Take a photograph in, clean it, and keep it.

    EXIF is stripped before anything is stored: a phone photograph of a house carries the
    house's coordinates, and this report goes to buyers, agents and lenders.
    """
    raw = await file.read()
    try:
        photo = clean(raw, filename=file.filename or "photo")
    except PhotoRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from inspection_report.domain.models import Photo

    record = Photo(
        sha256=photo.sha256,
        filename=file.filename or "photo",
        content_type=photo.content_type,
        size_bytes=photo.size_bytes,
        width=photo.width,
        height=photo.height,
    )
    db.save_photo(
        conn,
        finding_id=finding_id,
        photo=record,
        data=photo.data,
        thumbnail=photo.thumbnail,
    )
    return {
        "id": str(record.id),
        "sha256": photo.sha256,
        "stripped_exif": photo.stripped_exif,
        "width": photo.width,
        "height": photo.height,
    }


@app.get("/api/photos/{photo_id}")
def get_photo(photo_id: UUID, thumb: bool = False, conn: Any = Depends(get_conn)) -> Response:
    """Photographs are served from here, never by handing out the SuperDocs URL."""
    found = db.photo_bytes(conn, photo_id, thumbnail=thumb)
    if found is None:
        raise HTTPException(status_code=404, detail="no photograph with that id")
    data, mime = found
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=3600", "Referrer-Policy": "no-referrer"},
    )


@app.post("/api/check-phrasing")
def check_phrasing(body: dict[str, str]) -> dict[str, Any]:
    """Run the observational-language rail over a piece of text.

    Exposed so the interface can show an inspector the same verdict the gate will apply,
    while they are still typing, rather than after a rewrite has been proposed.
    """
    verdict = rail.check(body.get("text", ""))
    return {
        "clean": verdict.clean,
        "summary": verdict.summary(),
        "breaches": [
            {
                "rule_id": b.rule_id,
                "category": b.category,
                "matched": b.matched,
                "why": b.why,
                "suggest": b.suggest,
            }
            for b in verdict.breaches
        ],
    }


def _serialise(inspection: Inspection) -> dict[str, Any]:
    return {
        "id": str(inspection.id),
        "property": inspection.property.model_dump(),
        "inspector": inspection.inspector.model_dump(),
        "inspected_on": inspection.inspected_on.isoformat(),
        "template_key": inspection.template_key,
        "stage": str(inspection.stage),
        "findings": [
            {
                "id": str(f.id),
                "system_key": f.system_key,
                "severity_key": f.severity_key,
                "location": f.location,
                "observation": f.observation,
                "recommendation": f.recommendation,
                "plain_language": f.plain_language,
                # Photographs are referenced by our own id. The SuperDocs URL is never
                # handed to a browser.
                "photos": [
                    {"id": str(p.id), "caption": p.caption, "filename": p.filename}
                    for p in f.photos
                ],
            }
            for f in inspection.findings
        ],
    }


# --------------------------------------------------------- building the report
#
# The gate is three calls, not one, because a person holds it. `prepare` goes as far as the
# proposals and stops; `decisions` relays what the person decided; `export` produces the
# files and verifies them. Nothing holds a connection open waiting for a human.


# The fake stands in for a service that remembers sessions between requests, so it has to
# remember them too: one instance for the life of the process. A fresh one per request would
# lose the session between preparing a report and deciding on it — and would quietly make the
# offline application behave unlike the live one, which is the whole thing the fake exists to
# avoid. The live client is per-request because its state lives server-side.
_FAKE: Any = None


def _client() -> Any:
    global _FAKE
    from inspection_report.superdocs.fake import FakeSuperDocsClient
    from inspection_report.superdocs.live import LiveSuperDocsClient

    if os.environ.get("PROVIDER", "fake").lower() == "live":
        return LiveSuperDocsClient(api_key=os.environ.get("SUPERDOCS_API_KEY", ""))
    if _FAKE is None:
        _FAKE = FakeSuperDocsClient()
    return _FAKE


def _release(client: Any) -> None:
    """Close a live client; leave the shared fake open for the next request."""
    if client is not _FAKE:
        client.close()


def _session_id(inspection_id: UUID) -> str:
    """Derived, not stored: the same inspection always resumes the same session."""
    return f"inspection-{inspection_id.hex[:12]}"


def _template_html(key: str) -> str:
    """The local copy of a format, converted the way SuperDocs converts it.

    Formats are Word documents, so this is a conversion rather than a read. It is the
    fallback path: the report is normally built from the document SuperDocs hands back when
    the registered format is loaded.
    """
    path = TEMPLATE_DIR / f"{key}.docx"
    if not path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"unknown report format {key!r}. "
            f"Available: {sorted(p.stem for p in TEMPLATE_DIR.glob('*.docx'))}",
        )
    return docx_html.from_path(path)


def _load(conn: Any, inspection_id: UUID) -> Inspection:
    inspection = db.load_inspection(conn, inspection_id)
    if inspection is None:
        raise HTTPException(status_code=404, detail="no inspection with that id")
    return inspection


@app.post("/api/inspections/{inspection_id}/prepare")
def prepare_report(
    inspection_id: UUID,
    model_tier: str = "core",
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Render, upload, and ask for the rewrites. Stops at the gate."""
    from inspection_report.render import pipeline

    inspection = _load(conn, inspection_id)
    if not inspection.findings:
        raise HTTPException(
            status_code=400,
            detail="this inspection has no findings yet. Record at least one before "
            "preparing the report.",
        )

    client = _client()
    try:
        result = pipeline.prepare(
            inspection,
            _template_html(inspection.template_key),
            db.photo_data_for(conn, inspection_id),
            client,
            session_id=_session_id(inspection_id),
            known_uploads=db.known_uploads(conn),
            model_tier=model_tier,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SuperDocs: {exc}") from exc
    finally:
        _release(client)

    for finding in inspection.findings:
        for photo in finding.photos:
            if photo.remote_url:
                db.remember_upload(conn, sha256=photo.sha256, remote_url=photo.remote_url)

    db.clear_proposals(conn, inspection_id)
    db.record_proposals(
        conn,
        inspection_id=inspection_id,
        rows=[_proposal_row(result, p) for p in result.proposals],
    )
    db.save_inspection(conn, inspection)
    return _proposals_payload(result)


@app.get("/api/inspections/{inspection_id}/proposals")
def get_proposals(inspection_id: UUID, conn: Any = Depends(get_conn)) -> dict[str, Any]:
    """Whatever is currently at the gate.

    The interface needs this on load: a review is held open for as long as the person takes,
    and a refreshed browser must find the same queue rather than an empty screen.
    """
    rows = db.load_proposals(conn, inspection_id)
    return {
        "job_id": rows[0]["job_id"] if rows else "",
        "proposals": [
            {
                "change_id": r["change_id"],
                "before": r["old_text"],
                "after": r["new_text"],
                "rail_clean": r["rail_clean"],
                "breaches": r["rail_breaches"],
                "decision": r["decision"],
            }
            for r in rows
        ],
    }


@app.post("/api/inspections/{inspection_id}/decisions")
def submit_decisions(
    inspection_id: UUID, body: dict[str, bool], conn: Any = Depends(get_conn)
) -> dict[str, Any]:
    """Relay the person's decisions. One call per job, so every change is decided together."""
    from inspection_report.phrasing import rail as rail_mod
    from inspection_report.render import pipeline
    from inspection_report.superdocs.models import ChunkDiff

    inspection = _load(conn, inspection_id)
    rows = db.load_proposals(conn, inspection_id)
    if not rows:
        raise HTTPException(
            status_code=400, detail="nothing is awaiting a decision. Prepare the report first."
        )

    result = pipeline.PipelineResult(
        session_id=_session_id(inspection_id),
        document_html="",
        job_id=rows[0]["job_id"],
        proposals=[
            pipeline.Proposal(
                diff=ChunkDiff(
                    change_id=r["change_id"], old_html=r["old_text"], new_html=r["new_text"]
                ),
                verdict=rail_mod.check(r["new_text"]),
            )
            for r in rows
        ],
    )

    client = _client()
    try:
        pipeline.decide(inspection, result, client, approvals=body)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SuperDocs: {exc}") from exc
    finally:
        _release(client)

    db.record_proposals(
        conn,
        inspection_id=inspection_id,
        rows=[_proposal_row(result, p) for p in result.proposals],
    )
    db.save_inspection(conn, inspection)
    return _proposals_payload(result)


@app.post("/api/inspections/{inspection_id}/export")
def export_report(inspection_id: UUID, fmt: str = "pdf", conn: Any = Depends(get_conn)) -> Response:
    """Export, then read the finished file back and report what it actually contains."""
    from inspection_report.render import pipeline
    from inspection_report.templates import binding
    from inspection_report.verify import exports as verify_exports

    if fmt not in ("pdf", "docx"):
        raise HTTPException(status_code=400, detail="format must be pdf or docx")
    inspection = _load(conn, inspection_id)
    template = _template_html(inspection.template_key)

    client = _client()
    try:
        approved = [
            r["new_text"][:60]
            for r in db.load_proposals(conn, inspection_id)
            if r["decision"] == "approved"
        ]
        # Rebuilds the session's document from our own record if the session is gone, so a
        # finished report stays exportable across a restart. The flag says which path it
        # took; it is reported rather than swallowed.
        export, rebuilt = pipeline.export_recovering_session(
            client,
            inspection,
            template,
            session_id=_session_id(inspection_id),
            fmt=fmt,
            filename=_export_name(inspection),
            expected=approved,
            settle_s=pipeline.SETTLE_AFTER_APPROVE_S
            if os.environ.get("PROVIDER", "fake").lower() == "live"
            else 0.0,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SuperDocs: {exc}") from exc
    finally:
        _release(client)

    card = verify_exports.verify(
        data=export.content,
        fmt=fmt,
        inspection=inspection,
        expect_photos=binding.carries_photos(template, [s.name for s in catalogue.systems()]),
    )
    inspection.stage = ReportStage.EXPORTED
    db.save_inspection(conn, inspection)

    # The verification travels with the file rather than in a separate call, so a client
    # cannot hand someone the document without also having been told what is in it.
    return Response(
        content=export.content,
        media_type=export.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "X-Report-Verified": "pass" if card.passed else "fail",
            # Says how the document reached the export: from the live session, or rebuilt
            # from our own record because the session was gone. A caller should never have
            # to guess which, and the verification below applies either way.
            "X-Report-Source": "rebuilt" if rebuilt else "session",
            "X-Report-Checks": json.dumps(
                [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in card.checks]
            ),
        },
    )


def _export_name(inspection: Inspection) -> str:
    raw = f"{inspection.property.address_line}-{inspection.inspected_on:%Y-%m-%d}"
    return "".join(c if c.isalnum() else "-" for c in raw.lower()).strip("-")


def _proposal_row(result: Any, proposal: Any) -> dict[str, Any]:
    from uuid import uuid4

    return {
        "id": uuid4(),
        "finding_id": None,
        "job_id": result.job_id,
        "change_id": proposal.diff.change_id,
        "old_text": _strip(proposal.diff.old_html),
        "new_text": _strip(proposal.diff.new_html),
        "rail_clean": proposal.verdict.clean,
        "rail_breaches": [
            {"matched": b.matched, "category": b.category, "why": b.why, "suggest": b.suggest}
            for b in proposal.verdict.breaches
        ],
        "decision": ("approved" if proposal.approved else "rejected")
        if proposal.decided
        else "pending",
        "decided_at": None,
    }


def _proposals_payload(result: Any) -> dict[str, Any]:
    return {
        "session_id": result.session_id,
        "job_id": result.job_id,
        "photos_uploaded": result.photos_uploaded,
        "photos_reused": result.photos_reused,
        "ops_charged": result.ops_charged,
        "ops_remaining": result.ops_remaining,
        "proposals": [
            {
                "change_id": p.diff.change_id,
                "before": _strip(p.diff.old_html),
                "after": _strip(p.diff.new_html),
                "rail_clean": p.verdict.clean,
                "rail_summary": p.verdict.summary(),
                "approved": p.approved,
                "decided": p.decided,
                "breaches": [
                    {
                        "matched": b.matched,
                        "category": b.category,
                        "why": b.why,
                        "suggest": b.suggest,
                    }
                    for b in p.verdict.breaches
                ],
            }
            for p in result.proposals
        ],
    }


def _strip(html: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", html).strip()
