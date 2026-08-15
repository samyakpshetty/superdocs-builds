"""Typed models for the SuperDocs surface this build depends on.

Field names match the live API verbatim, so the client stays a thin, honest mapping. Where a
name differs from the published documentation, the difference is recorded in a comment and
was established by calling the real endpoint — the documentation and the API disagree in
several places and the API is what ships.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ImageUpload(BaseModel):
    """The result of uploading one photograph.

    Two URLs come back, and the difference matters. ``url`` is stable and permanent;
    ``view_url`` is the same object with a 24-hour signature on it. I verified that ``url``
    is readable with **no credentials at all**, so it is a capability, not an identifier:
    treat it as a secret, never log it, never commit it. Only ``url`` is persisted, because
    ``view_url`` would be stale within a day.
    """

    model_config = {"extra": "ignore"}

    url: str
    content_type: str = ""
    size: int = 0

    def __repr__(self) -> str:  # pragma: no cover - defensive, mirrors __str__
        return self.__str__()

    def __str__(self) -> str:
        """Never render the URL. A stray f-string must not leak a photograph."""
        return f"ImageUpload(size={self.size}, content_type={self.content_type!r}, url=<redacted>)"


class TemplateRef(BaseModel):
    """A registered report format, as SuperDocs lists it."""

    model_config = {"extra": "ignore"}

    id: str
    name: str
    file_extension: str = ""
    file_size: int = 0
    created_at: str = ""


class ExportOptions(BaseModel):
    """Export customisation. Mirrors the API's ``options`` object exactly."""

    paper_size: str = "Letter"
    orientation: str = "portrait"
    margins: str = "normal"
    filename: str | None = None
    # HTML export only. True embeds image bytes rather than referencing them by URL, which
    # is why this build sets it: an exported HTML file must not carry capability URLs.
    embed_images: bool = False
    watermark_text: str | None = None


class ChunkDiff(BaseModel):
    """One proposed edit to one chunk.

    ``change_id`` is what ``approve`` keys on. Approving by ``chunk_id`` returns a 500 — this
    was established against the live API and contradicts the published documentation.

    Not every edit is a replacement. A **deletion** arrives with ``new_html: null`` and an
    **insertion** with ``old_html: null``, so the side that does not exist is absent rather
    than empty. Declaring these as plain ``str`` made the whole job unparseable the first time
    the AI was asked to remove a section: one null field, and a completed job that had already
    been paid for could not be read. Null is normalised to the empty string here, in one
    place, so nothing downstream has to know — and ``operation`` still says which kind of
    edit it is.
    """

    model_config = {"extra": "ignore"}

    chunk_id: str = ""
    change_id: str = ""
    operation: str = ""
    old_html: str = ""
    new_html: str = ""
    chunk_type: str = ""
    ai_explanation: str = ""

    @field_validator(
        "chunk_id",
        "change_id",
        "operation",
        "old_html",
        "new_html",
        "chunk_type",
        "ai_explanation",
        mode="before",
    )
    @classmethod
    def _null_is_absent(cls, value: Any) -> Any:
        return "" if value is None else value

    @property
    def is_deletion(self) -> bool:
        """The change removes content rather than replacing it."""
        return bool(self.old_html) and not self.new_html


class Usage(BaseModel):
    """What a request actually cost, and what is left.

    This is the only reliable meter: ``/v1/users/me/usage`` rejects API keys outright, and
    ``whoami`` reports the subscription quota while omitting the promotional bucket
    entirely. The true remaining balance rides back on every chat job.
    """

    model_config = {"extra": "ignore"}

    ops_charged: int = 0
    was_billable: bool = False
    quota_exhausted: bool = False
    bucket_used: str = ""
    monthly_used: int = 0
    monthly_limit: int = 0
    monthly_remaining: int = 0
    promotions: list[dict[str, Any]] = Field(default_factory=list)

    def ops_remaining(self) -> int:
        """Remaining operations, preferring the promotional bucket when one is active."""
        for promo in self.promotions:
            remaining = promo.get("ops_remaining")
            if isinstance(remaining, int):
                return remaining
        return self.monthly_remaining


class Job(BaseModel):
    """An async chat job.

    ``document_html`` is where a completed job's document actually lives once assembled from
    the response: the top-level ``document_html`` field is ``None`` on a completed job and
    the content sits at ``result.document_changes.updated_html``.
    """

    model_config = {"extra": "ignore"}

    job_id: str
    status: JobStatus
    chunk_diffs: list[ChunkDiff] = Field(default_factory=list)
    document_html: str | None = None
    requires_approval: bool = False
    usage: Usage | None = None
    error: str | None = None


class UploadResult(BaseModel):
    model_config = {"extra": "ignore"}

    html: str = ""
    session_id: str = ""
    chunks_count: int = 0
    version_id: str = ""


class ApprovalDecision(BaseModel):
    """One item in the item-by-item human gate."""

    change_id: str
    approved: bool
    feedback: str = ""


class ApproveResult(BaseModel):
    model_config = {"extra": "ignore"}

    status: str = ""
    applied_count: int = 0
    denied_count: int = 0
    job_id: str | None = None


@dataclass
class ExportResult:
    """A finished file. Bytes, not a model, because it is binary content."""

    content: bytes
    content_type: str
    filename: str
    warnings: list[dict[str, Any]] = field(default_factory=list)
