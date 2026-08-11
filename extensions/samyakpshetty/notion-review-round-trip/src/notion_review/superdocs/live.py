"""Live SuperDocs client over HTTP — the four core calls plus the async job poll.

A thin, honest mapping onto the documented REST API. Transient failures (429, 5xx, timeouts) are
retried with exponential backoff and jitter, honouring ``Retry-After``; permanent 4xx fail fast.
Proposed changes come back through the shared ``parse_pending_changes`` helper, so the documented
JSON-string double-decode is handled in exactly one place for both the fake and this client.
"""

from __future__ import annotations

import contextlib
import random
import time
from typing import Any

import httpx

from notion_review.config import Config
from notion_review.logging import get_logger
from notion_review.superdocs.base import parse_pending_changes
from notion_review.superdocs.models import (
    ApprovalDecision,
    ApproveResult,
    ExportResult,
    Job,
    JobStatus,
    UploadResult,
    Usage,
)

_log = get_logger("notion_review.superdocs.live")
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class SuperDocsError(Exception):
    """A SuperDocs API call failed."""


class LiveSuperDocsClient:
    """HTTP implementation of :class:`~notion_review.superdocs.base.SuperDocsClient`."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.superdocs.app",
        config: Config | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0),
        )
        self._max_retries = config.superdocs_max_retries if config else 5
        self._backoff = config.superdocs_backoff_base_s if config else 0.5

    def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        attempt = 0
        while True:
            try:
                resp = self._client.request(method, path, json=json)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise SuperDocsError(f"{method} {path}: {exc}") from exc
                self._sleep(attempt, None)
                attempt += 1
                continue
            if resp.status_code in _RETRYABLE and attempt < self._max_retries:
                self._sleep(attempt, resp.headers.get("Retry-After"))
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise SuperDocsError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
            return resp

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        delay = self._backoff * (2**attempt) + random.uniform(0, self._backoff)
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = float(retry_after)
        time.sleep(delay)

    def upload_document(self, *, document_html: str, session_id: str) -> UploadResult:
        data = self._request(
            "POST",
            "/v1/documents/upload-base64",
            json={"document_html": document_html, "session_id": session_id, "return_html": True},
        ).json()
        return UploadResult(
            html=data.get("html", ""),
            session_id=data.get("session_id", session_id),
            chunks_count=data.get("chunks_count", 0),
            version_id=data.get("version_id", ""),
        )

    def chat_async(
        self,
        *,
        session_id: str,
        message: str,
        document_html: str | None = None,
        approval_mode: str = "ask_every_time",
    ) -> str:
        body: dict[str, Any] = {
            "message": message,
            "session_id": session_id,
            "approval_mode": approval_mode,
        }
        if document_html is not None:
            body["document_html"] = document_html
        data = self._request("POST", "/v1/chat/async", json=body).json()
        return str(data["job_id"])

    def get_job(self, job_id: str) -> Job:
        data = self._request("GET", f"/v1/jobs/{job_id}").json()
        metadata = data.get("metadata") or {}
        usage = data.get("usage")
        return Job(
            job_id=data.get("job_id", job_id),
            status=_status(data.get("status")),
            chunk_diffs=parse_pending_changes(metadata),
            document_html=data.get("document_html"),
            usage=Usage.model_validate(usage) if usage else None,
        )

    def approve(self, *, session_id: str, decisions: list[ApprovalDecision]) -> ApproveResult:
        body = {
            "changes": [
                {"chunk_id": d.chunk_id, "approved": d.approved, "feedback": d.feedback}
                for d in decisions
            ]
        }
        data = self._request("POST", f"/v1/chat/{session_id}/approve", json=body).json()
        return ApproveResult(
            status=data.get("status", ""),
            applied_count=data.get("applied_count", 0),
            denied_count=data.get("denied_count", 0),
            job_id=data.get("job_id"),
        )

    def export(self, *, session_id: str, fmt: str = "docx") -> ExportResult:
        resp = self._request(
            "POST", "/v1/documents/export", json={"session_id": session_id, "format": fmt}
        )
        return ExportResult(
            content=resp.content,
            content_type=resp.headers.get("content-type", _DOCX_MIME),
            filename=f"{session_id}.{fmt}",
        )

    def close(self) -> None:
        self._client.close()


def _status(value: Any) -> JobStatus:
    try:
        return JobStatus(value)
    except (ValueError, TypeError):
        return JobStatus.PROCESSING
