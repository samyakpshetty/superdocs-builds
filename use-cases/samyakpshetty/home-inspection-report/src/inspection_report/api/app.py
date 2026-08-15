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

import os
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from inspection_report.domain import catalogue
from inspection_report.domain.models import Finding, Inspection, Inspector, Property
from inspection_report.logging import get_logger, setup_logging
from inspection_report.photos.pipeline import PhotoRejected, clean
from inspection_report.phrasing import rail
from inspection_report.store import db

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
        "formats": sorted(p.stem for p in TEMPLATE_DIR.glob("*.html")),
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

    if body.template_key not in {p.stem for p in TEMPLATE_DIR.glob("*.html")}:
        raise HTTPException(
            status_code=400,
            detail=f"unknown report format {body.template_key!r}. "
            f"Available: {sorted(p.stem for p in TEMPLATE_DIR.glob('*.html'))}",
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
