"""A deterministic, in-memory SuperDocs stand-in.

It is a faithful implementation of the client contract, not a mock:

* upload parses the document and assigns a ``data-chunk-id`` to every block-level element,
  preserving any attributes we sent (so our block markers survive, exactly as the real service
  preserves them);
* an edit is matched to a chunk by its content, applied to the in-memory document tree, and
  returned through the async job + item-by-item approval flow;
* proposed changes are emitted as a JSON-encoded string inside job metadata, so the double-parse
  in :func:`~notion_review.superdocs.base.parse_pending_changes` runs for real in offline tests;
* export renders the current document to a genuine .docx;
* operations are metered so budget and cost logic is exercised without spending a cent.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import cast

from lxml import html as lxml_html
from lxml.html import HtmlElement

from notion_review.domain import ChangeOperation
from notion_review.superdocs.base import parse_pending_changes
from notion_review.superdocs.chunking import iter_block_elements
from notion_review.superdocs.docx import html_to_docx
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
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _norm(text: str) -> str:
    return " ".join(text.split()).strip()


def _text(el: HtmlElement) -> str:
    return str(cast(HtmlElement, el).text_content())


@dataclass
class _Session:
    root: HtmlElement  # the <body> wrapper; its children are the top-level blocks
    version: int = 1

    def blocks(self) -> list[HtmlElement]:
        return list(iter_block_elements(self.root))

    def by_chunk(self, chunk_id: str) -> HtmlElement | None:
        return next((el for el in self.blocks() if el.get(_CHUNK_ATTR) == chunk_id), None)

    def by_text(self, needle: str) -> HtmlElement | None:
        target = _norm(needle)
        if not target:
            return None
        matches = [el for el in self.blocks() if target in _norm(_text(el))]
        # Prefer the most specific block (shortest text) if several contain the passage.
        return min(matches, key=lambda el: len(_norm(_text(el))), default=None)

    def html(self) -> str:
        return "".join(lxml_html.tostring(c, encoding="unicode") for c in self.root)


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
        session = _Session(root=root)
        count = 0
        for el in iter_block_elements(root):
            el.set(_CHUNK_ATTR, f"chunk_{count:04d}")
            count += 1
        self._sessions[session_id] = session
        return UploadResult(
            html=session.html(),
            session_id=session_id,
            chunks_count=count,
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
            target = session.by_text(parsed.find_text)
            if target is not None:
                diffs.append(self._diff_for(target, parsed.operation, parsed.replace_text))

        # One operation per edit request (real API: 1 op per <=25 sections). A request that
        # produces no change costs nothing, mirroring the free-when-nothing-changed rule.
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

    def _diff_for(
        self, el: HtmlElement, operation: ChangeOperation, replace_text: str
    ) -> ChunkDiff:
        chunk_id = el.get(_CHUNK_ATTR, "")
        old_html = lxml_html.tostring(el, encoding="unicode")
        if operation == ChangeOperation.DELETE:
            new_html = ""
        else:
            clone = deepcopy(el)
            for child in list(clone):
                clone.remove(child)
            clone.text = replace_text
            new_html = lxml_html.tostring(clone, encoding="unicode")
        return ChunkDiff(
            chunk_id=chunk_id,
            operation=operation.value,
            old_html=old_html,
            new_html=new_html,
            chunk_type=str(el.tag),
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
        el = session.by_chunk(diff.chunk_id)
        if el is None:
            return
        if diff.operation == ChangeOperation.DELETE.value:
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
        else:
            new_text = _text(lxml_html.fromstring(diff.new_html)) if diff.new_html else ""
            for child in list(el):
                el.remove(child)
            el.text = new_text
        session.version += 1

    def export(self, *, session_id: str, fmt: str = "docx") -> ExportResult:
        session = self._sessions.get(session_id)
        html = session.html() if session else ""
        return ExportResult(
            content=html_to_docx(html),
            content_type=_DOCX_MIME,
            filename=f"{session_id}.{fmt}",
        )

    # -- test/introspection helpers (not part of the wire contract) ------------
    def monthly_used(self) -> int:
        return self._monthly_used

    def session_html(self, session_id: str) -> str:
        session = self._sessions.get(session_id)
        return session.html() if session else ""
