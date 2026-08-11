"""A deterministic, in-memory SuperDocs stand-in.

It is a faithful implementation of the client contract, not a mock:

* upload assigns a stable ``data-chunk-id`` to every top-level block, exactly as SuperDocs does;
* an edit is matched to a chunk by its content (as the real service does), applied to the
  session document, and returned through the async job + item-by-item approval flow;
* proposed changes are emitted as a JSON-encoded string inside job metadata, so the double-parse
  in :func:`~notion_review.superdocs.base.parse_pending_changes` runs for real in offline tests;
* operations are metered so budget and cost logic is exercised without spending a cent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import cast

from lxml import html as lxml_html
from lxml.html import HtmlElement

from notion_review.domain import ChangeOperation
from notion_review.superdocs.base import parse_pending_changes
from notion_review.superdocs.instructions import parse_instruction
from notion_review.superdocs.models import (
    ApprovalDecision,
    ApproveResult,
    ChunkDiff,
    ExportResult,
    Job,
    JobStatus,
    UploadResult,
    Usage,
)

_CHUNK_ATTR = "data-chunk-id"


def _norm(text: str) -> str:
    return " ".join(text.split()).strip()


@dataclass
class _Chunk:
    chunk_id: str
    tag: str
    html: str
    text: str


@dataclass
class _Session:
    chunks: list[_Chunk] = field(default_factory=list)
    version: int = 0

    def find_by_text(self, needle: str) -> _Chunk | None:
        target = _norm(needle)
        if not target:
            return None
        for chunk in self.chunks:
            if target in _norm(chunk.text):
                return chunk
        return None

    def chunk(self, chunk_id: str) -> _Chunk | None:
        return next((c for c in self.chunks if c.chunk_id == chunk_id), None)


@dataclass
class _JobRecord:
    job_id: str
    session_id: str
    status: JobStatus
    metadata: dict[str, str]  # pending_changes stored as a JSON string, like the real API
    ops_charged: int


class FakeSuperDocsClient:
    """In-memory implementation of :class:`~notion_review.superdocs.base.SuperDocsClient`."""

    def __init__(self, *, monthly_limit: int = 10_000, quota_used: int = 0) -> None:
        self._sessions: dict[str, _Session] = {}
        self._jobs: dict[str, _JobRecord] = {}
        self._monthly_limit = monthly_limit
        self._monthly_used = quota_used
        self._counter = 0

    # -- helpers ---------------------------------------------------------------
    def _next(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter:04d}"

    def _usage(self, ops_charged: int) -> Usage:
        return Usage(
            monthly_used=self._monthly_used,
            monthly_limit=self._monthly_limit,
            ops_charged=ops_charged,
            was_billable=ops_charged > 0,
            quota_exhausted=self._monthly_used >= self._monthly_limit,
        )

    # -- contract --------------------------------------------------------------
    def upload_document(self, *, document_html: str, session_id: str) -> UploadResult:
        root = lxml_html.fromstring(f"<body>{document_html}</body>")
        chunks: list[_Chunk] = []
        for i, el in enumerate(root):
            chunk_id = f"chunk_{i:04d}"
            el.set(_CHUNK_ATTR, chunk_id)
            html = lxml_html.tostring(el, encoding="unicode")
            text = cast(HtmlElement, el).text_content()
            chunks.append(_Chunk(chunk_id=chunk_id, tag=str(el.tag), html=html, text=text))
        session = _Session(chunks=chunks, version=1)
        self._sessions[session_id] = session
        annotated = "".join(c.html for c in chunks)
        return UploadResult(
            html=annotated,
            session_id=session_id,
            chunks_count=len(chunks),
            version_id=self._next("v"),
        )

    def chat_async(
        self,
        *,
        session_id: str,
        message: str,
        document_html: str | None = None,
        approval_mode: str = "ask_every_time",
    ) -> str:
        if document_html is not None and session_id not in self._sessions:
            self.upload_document(document_html=document_html, session_id=session_id)
        session = self._sessions.get(session_id)
        job_id = self._next("job")

        diffs: list[ChunkDiff] = []
        parsed = parse_instruction(message)
        if session is not None and parsed is not None:
            match = session.find_by_text(parsed.find_text)
            if match is not None:
                diffs.append(self._diff_for(match, parsed.operation, parsed.replace_text))

        # One operation per edit request (real API: 1 op per <=25 sections). Billed even at
        # the gate, because the model work happened; a fully no-op request charges nothing.
        ops = 1 if diffs else 0
        self._monthly_used += ops
        status = JobStatus.AWAITING_APPROVAL if diffs else JobStatus.COMPLETED
        metadata = {"pending_changes": json.dumps([d.model_dump() for d in diffs])}
        self._jobs[job_id] = _JobRecord(
            job_id=job_id,
            session_id=session_id,
            status=status,
            metadata=metadata,
            ops_charged=ops,
        )
        return job_id

    def _diff_for(self, chunk: _Chunk, operation: ChangeOperation, replace_text: str) -> ChunkDiff:
        if operation == ChangeOperation.DELETE:
            new_html = ""
        else:
            open_tag = f'<{chunk.tag} {_CHUNK_ATTR}="{chunk.chunk_id}">'
            new_html = f"{open_tag}{replace_text}</{chunk.tag}>"
        return ChunkDiff(
            chunk_id=chunk.chunk_id,
            operation=operation.value,
            old_html=chunk.html,
            new_html=new_html,
            chunk_type=chunk.tag,
        )

    def get_job(self, job_id: str) -> Job:
        record = self._jobs[job_id]
        # Parse pending_changes exactly as a caller must against the real API (double-decode).
        diffs = parse_pending_changes(record.metadata)
        return Job(
            job_id=record.job_id,
            status=record.status,
            chunk_diffs=diffs,
            usage=self._usage(record.ops_charged),
        )

    def approve(self, *, session_id: str, decisions: list[ApprovalDecision]) -> ApproveResult:
        session = self._sessions.get(session_id)
        applied = denied = 0
        by_chunk = {d.chunk_id: d for d in decisions}
        # Apply approved diffs from any awaiting job on this session.
        for record in self._jobs.values():
            if record.session_id != session_id or record.status != JobStatus.AWAITING_APPROVAL:
                continue
            for diff in parse_pending_changes(record.metadata):
                decision = by_chunk.get(diff.chunk_id)
                if decision is None:
                    continue
                if decision.approved:
                    self._apply(session, diff)
                    applied += 1
                else:
                    denied += 1
            record.status = JobStatus.COMPLETED
        return ApproveResult(status="changes_applied", applied_count=applied, denied_count=denied)

    def _apply(self, session: _Session | None, diff: ChunkDiff) -> None:
        if session is None:
            return
        chunk = session.chunk(diff.chunk_id)
        if chunk is None:
            return
        if diff.operation == ChangeOperation.DELETE.value:
            session.chunks = [c for c in session.chunks if c.chunk_id != diff.chunk_id]
        else:
            chunk.html = diff.new_html
            chunk.text = _norm(
                cast(HtmlElement, lxml_html.fromstring(diff.new_html)).text_content()
            )
        session.version += 1

    def export(self, *, session_id: str, fmt: str = "docx") -> ExportResult:
        session = self._sessions.get(session_id)
        body = "".join(c.html for c in session.chunks) if session else ""
        # A minimal but real HTML document; F3 renders a true .docx for the reviewer copy.
        html = f"<!doctype html><html><body>{body}</body></html>"
        return ExportResult(
            content=html.encode("utf-8"),
            content_type="text/html",
            filename=f"{session_id}.{fmt}",
        )

    # -- test/introspection helpers (not part of the wire contract) ------------
    def monthly_used(self) -> int:
        return self._monthly_used

    def session_html(self, session_id: str) -> str:
        session = self._sessions.get(session_id)
        return "".join(c.html for c in session.chunks) if session else ""
