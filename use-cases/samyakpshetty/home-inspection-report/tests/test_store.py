"""The database, against a real Postgres.

Marked ``postgres`` and skipped when there is no database, so the keyless suite stays keyless
and a stranger's `make check` is unaffected. `make test-db` runs these.

This file exists because of a bug that could not have been caught anywhere else. Saving an
inspection cleared its findings and re-inserted them, and ``photos.finding_id`` is
``ON DELETE CASCADE`` — so every photograph on the inspection was destroyed by the ordinary
act of recording the next finding. No amount of testing against the in-memory fake would have
seen it: the cascade is a property of the schema, not of the code.
"""

from __future__ import annotations

import pytest

from inspection_report import sample
from inspection_report.domain.models import Finding
from inspection_report.photos.pipeline import clean
from inspection_report.store import db

pytestmark = pytest.mark.postgres


@pytest.fixture
def conn():  # type: ignore[no-untyped-def]
    """A database, or a skip.

    `DATABASE_URL` is set by compose even when the db service is not running, so its presence
    proves nothing — the only honest check is whether a connection opens. Without that, a
    stranger running `make check` on a fresh clone gets four errors instead of a green run,
    which is the opposite of the promise the README makes.
    """
    try:
        with db.connect():
            pass
    except Exception as exc:
        # Any connection failure means "no database here", so the kind does not matter.
        pytest.skip(f"needs a Postgres ({type(exc).__name__}); try `make test-db`")
    with db.connect() as connection:
        db.apply_schema(connection)
        yield connection


def _seeded(connection) -> object:  # type: ignore[no-untyped-def]
    """One inspection with a photograph on its first finding."""
    inspection = sample.sample_inspection()
    inspection.findings = inspection.findings[:2]
    db.save_inspection(connection, inspection)
    data = sample.sample_photo_data()
    first = inspection.findings[0]
    photo = first.photos[0]
    cleaned = clean(data[photo.filename], filename=photo.filename)
    db.save_photo(
        connection,
        finding_id=first.id,
        photo=photo,
        data=cleaned.data,
        thumbnail=cleaned.thumbnail,
    )
    return inspection


def _photo_count(connection, inspection) -> int:  # type: ignore[no-untyped-def]
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM photos p JOIN findings f ON f.id = p.finding_id "
            "WHERE f.inspection_id = %s",
            (inspection.id,),
        )
        row = cur.fetchone()
    return int(row["n"])


class TestPhotographsSurviveAnOrdinaryDay:
    def test_recording_another_finding_keeps_the_earlier_photograph(self, conn) -> None:  # type: ignore[no-untyped-def]
        inspection = _seeded(conn)
        assert _photo_count(conn, inspection) == 1

        inspection.findings.append(
            Finding(system_key="roof", severity_key="monitor", observation="ridge tiles seated")
        )
        db.save_inspection(conn, inspection)
        assert _photo_count(conn, inspection) == 1, "adding a finding destroyed a photograph"

    def test_saving_the_same_inspection_twice_keeps_it(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Approving decisions and exporting both save on their way through."""
        inspection = _seeded(conn)
        db.save_inspection(conn, inspection)
        db.save_inspection(conn, inspection)
        assert _photo_count(conn, inspection) == 1

    def test_an_edited_finding_is_updated_not_replaced(self, conn) -> None:  # type: ignore[no-untyped-def]
        inspection = _seeded(conn)
        inspection.findings[0].plain_language = "Found a gap in the flashing at the chimney."
        db.save_inspection(conn, inspection)

        reloaded = db.load_inspection(conn, inspection.id)
        assert reloaded is not None
        assert reloaded.findings[0].plain_language.startswith("Found a gap")
        assert _photo_count(conn, inspection) == 1

    def test_a_removed_finding_really_goes(self, conn) -> None:  # type: ignore[no-untyped-def]
        """The upsert must not become a leak: deleting a finding still deletes it."""
        inspection = _seeded(conn)
        keep = inspection.findings[0]
        inspection.findings = [keep]
        db.save_inspection(conn, inspection)

        reloaded = db.load_inspection(conn, inspection.id)
        assert reloaded is not None
        assert [f.id for f in reloaded.findings] == [keep.id]
