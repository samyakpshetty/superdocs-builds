"""A deterministic, in-memory SuperDocs.

This is what makes the whole application run on a fresh clone with no API key, and it is what
the test suite exercises. Two rules govern it:

1. **It is never more forgiving than the live service.** Same 10 MB image cap, same accepted
   MIME types, one ``approve`` call per job, ``session_busy`` while proposals are pending,
   the same "changes arrive as a JSON-encoded string" shape. A fake that accepts what the
   API rejects hides precisely the bugs it exists to catch.
2. **Its edits misbehave the way the real AI misbehaves.** Asked to rewrite an inspector's
   note, it produces certification language on some inputs — because the live service did
   exactly that on this build's first sample. If the offline suite only ever saw well-behaved
   output, the language rail would never be exercised where it matters.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field

from inspection_report.superdocs.base import (
    ALLOWED_IMAGE_TYPES,
    MAX_IMAGE_BYTES,
    SessionBusyError,
    SuperDocsError,
    parse_pending_changes,
)
from inspection_report.superdocs.models import (
    ApprovalDecision,
    ApproveResult,
    ChunkDiff,
    ExportOptions,
    ExportResult,
    ImageUpload,
    Job,
    JobStatus,
    TemplateRef,
    UploadResult,
    Usage,
)
from inspection_report.superdocs.offline_export import ImageResolver, to_docx, to_pdf

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_BLOCK = re.compile(r"<(h[1-4]|p|li|blockquote)(\s[^>]*)?>", re.IGNORECASE)
# Everything `data-*` except the service's own chunk id, which is what upload adds.
_CUSTOM_DATA_ATTR = re.compile(r'\s+data-(?!chunk-id\b)[\w-]+="[^"]*"', re.IGNORECASE)
# SuperDocs parses a document into structured chunks and drops HTML comments on the way:
# verified live, four sent and none returned. Anything that has to survive a round trip
# cannot be a comment.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# Mirrors the live bucket, so redaction and capability-URL handling are exercised offline
# against a realistically shaped URL rather than a placeholder.
_IMAGE_HOST = "https://storage.googleapis.com/superdocs-document-images/img"

# Rewrites the fake proposes. The first two are deliberately unsafe: they reproduce the
# certification language the live service actually emitted, so the rail is exercised.
_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bgfci\b", re.IGNORECASE),
        "The kitchen GFCI is not functioning correctly. Recommend replacement for safety.",
    ),
    (
        re.compile(r"\bcrack\b", re.IGNORECASE),
        "A minor settling crack was noted, typical for the home's age.",
    ),
)


# Field shorthand -> plain English. Ordered, applied in sequence.
_EXPANSIONS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), r)
    for p, r in (
        (r"^S\b", "south"),
        (r"^N\b", "north"),
        (r"\bdbl\b", "double"),
        (r"\borig\b", "original"),
        (r"\bapprox\b", "approximately"),
        (r"~", "approximately "),
        (r"\s*@\s*", " at the "),
        (r"(\d+)\s*in\b", r"\1 inches"),
        (r"(\d+)\s*ft\b", r"\1 feet"),
        (r"\bw/\s*", "with "),
        (r"\bno\b", "no"),
        (r",\s*", ", "),
    )
)


def _requested_template(message: str) -> str | None:
    """The one chat instruction that means "give me a saved format", not "edit this document"."""
    m = re.search(r"load my ['\"](.+?)['\"] template", message, re.IGNORECASE)
    return m.group(1) if m else None


@dataclass
class _Session:
    html: str = ""
    version: int = 0


@dataclass
class _Job:
    job_id: str
    session_id: str
    status: JobStatus
    diffs: list[ChunkDiff]
    approved: bool = False  # approving closes the job; a second call is refused
    document_html: str = ""


@dataclass
class FakeSuperDocsClient:
    """In-memory implementation of the same protocol the live client satisfies."""

    sessions: dict[str, _Session] = field(default_factory=dict)
    jobs: dict[str, _Job] = field(default_factory=dict)
    templates: dict[str, TemplateRef] = field(default_factory=dict)
    template_bytes: dict[str, bytes] = field(default_factory=dict)
    images: ImageResolver = field(default_factory=ImageResolver)
    ops_charged: int = 0
    ops_budget: int = 10_000
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def _next(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):04d}"

    # ------------------------------------------------------- the minimum contract

    def upload_document(self, *, document_html: str, session_id: str) -> UploadResult:
        counter = itertools.count(1)

        def stamp(m: re.Match[str]) -> str:
            tag, attrs = m.group(1), m.group(2) or ""
            # The live service drops custom `data-*` attributes on upload while keeping
            # `class`. Verified against the API, and reproduced here: a fake that carried
            # our own data attributes through would let a design depend on something the
            # real service throws away.
            attrs = _CUSTOM_DATA_ATTR.sub("", attrs)
            if "data-chunk-id" in attrs:
                return f"<{tag}{attrs}>"
            # Deterministic ids: the same document always produces the same chunk ids, so a
            # test can assert on them without being brittle.
            cid = hashlib.sha1(f"{session_id}:{next(counter)}".encode()).hexdigest()[:32]
            return f'<{tag}{attrs} data-chunk-id="{cid}">'

        html = _BLOCK.sub(stamp, _HTML_COMMENT.sub("", document_html))
        session = self.sessions.setdefault(session_id, _Session())
        session.html = html
        session.version += 1
        return UploadResult(
            html=html,
            session_id=session_id,
            chunks_count=len(re.findall(r"data-chunk-id=", html)),
            version_id=f"v{session.version}",
        )

    def chat_async(
        self,
        *,
        session_id: str,
        message: str,
        approval_mode: str = "ask_every_time",
        model_tier: str = "core",
        thinking_depth: str = "balanced",
    ) -> str:
        wanted = _requested_template(message)
        if wanted is not None:
            # Loading a saved template is how a report format actually reaches a session:
            # there is no endpoint that applies one, and the live AI does exactly this when
            # asked for a template by name. A session need not hold a document yet.
            return self._load_template(session_id=session_id, name=wanted)

        session = self.sessions.get(session_id)
        if session is None:
            raise SuperDocsError(
                f"no document loaded in session {session_id!r}. Upload a document first."
            )
        # A session holds ONE pending proposal set. The live API answers a second request
        # with a 409 that never clears, so the fake refuses it the same way.
        for job in self.jobs.values():
            if job.session_id == session_id and job.status is JobStatus.AWAITING_APPROVAL:
                raise SessionBusyError(
                    f"session {session_id!r} already holds proposals awaiting approval "
                    f"(job {job.job_id}). Decide them before sending another batch."
                )

        diffs = self._propose(session.html)
        self.ops_charged += 1
        job_id = self._next("job")
        self.jobs[job_id] = _Job(
            job_id=job_id,
            session_id=session_id,
            status=JobStatus.AWAITING_APPROVAL
            if approval_mode == "ask_every_time"
            else JobStatus.COMPLETED,
            diffs=diffs,
            document_html=session.html,
        )
        return job_id

    def _load_template(self, *, session_id: str, name: str) -> str:
        """Serve a registered template back as the session's document.

        Refuses a name it does not hold rather than inventing a document, because a report
        built on an unrecognised skeleton is worse than a report that failed to build.
        """
        match = next((t for t in self.templates.values() if t.name.startswith(name)), None)
        if match is None:
            raise SuperDocsError(
                f"no saved template matching {name!r}. Register the format before asking for it."
            )
        # Served the way the live service serves it. A format is a Word document, so this is
        # a conversion — and the live service returns the document exactly as saved, which
        # was measured on a real round trip rather than assumed. Comments are stripped for
        # the case where a caller registers HTML: the parser drops them, and a fake that kept
        # them would let a format work offline and fail live.
        raw = self.template_bytes[match.id]
        if raw[:2] == b"PK":
            from inspection_report.templates import docx_html

            html = docx_html.from_bytes(raw)
        else:
            html = _HTML_COMMENT.sub("", raw.decode("utf-8"))
        self.ops_charged += 1
        job_id = self._next("job")
        self.sessions.setdefault(session_id, _Session()).html = html
        self.jobs[job_id] = _Job(
            job_id=job_id,
            session_id=session_id,
            status=JobStatus.COMPLETED,
            diffs=[],
            approved=True,
            document_html=html,
        )
        return job_id

    def _propose(self, html: str) -> list[ChunkDiff]:
        """One proposed rewrite per findings paragraph, deterministic in content.

        The proposals are assembled as the **wire shape** and handed to the same parser the
        live client uses, rather than constructed as typed objects directly. Building them
        directly left the parser exercised only against the live service, which is how a
        change carrying ``new_html: null`` took down a job that had already been paid for:
        offline, nothing ever went through that code path. Routing the fake through it means
        a parse that would fail live now fails in the keyless suite instead.
        """
        items: list[dict[str, object]] = []
        # Attribute order is not guaranteed — stamping appends data-chunk-id after whatever
        # the renderer already wrote — so match the tag and read its attributes, rather than
        # assuming one ordering and silently proposing nothing when it differs.
        for m in re.finditer(r"<p([^>]*)>(.*?)</p>", html, re.IGNORECASE | re.DOTALL):
            attrs, inner = m.group(1), m.group(2)
            if "finding-note" not in attrs:
                continue
            id_match = re.search(r'data-chunk-id="([^"]+)"', attrs)
            if not id_match:
                continue
            chunk_id = id_match.group(1)
            text = re.sub(r"<[^>]+>", "", inner).strip()
            if not text:
                continue
            new = self._rewrite(text)
            if new == text:
                continue
            items.append(
                {
                    "chunk_id": chunk_id,
                    "change_id": f"chg-{chunk_id[:12]}",
                    "operation": "replace",
                    "old_html": inner,
                    "new_html": new,
                    "chunk_type": "paragraph",
                    # Null, not "": the live service omits this on some changes, and a fake
                    # that always supplies a string would hide that from the parser.
                    "ai_explanation": None,
                }
            )
        # The double encoding is the live shape too: `pending_changes` arrives as a
        # JSON-encoded string that needs a second parse.
        return parse_pending_changes({"metadata": {"pending_changes": json.dumps(items)}})

    def _rewrite(self, text: str) -> str:
        for pattern, replacement in _REWRITES:
            if pattern.search(text):
                return replacement
        # The well-behaved path: expand the shorthand an inspector actually types into a
        # sentence a buyer can read. Deterministic, and close enough to what the live
        # service produces that the offline demo shows the real transformation rather than
        # a placeholder.
        out = text.rstrip(" .")
        for pattern, replacement in _EXPANSIONS:
            out = pattern.sub(replacement, out)
        out = " ".join(out.split())
        return f"Found {out[0].lower()}{out[1:]}."

    def get_job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise SuperDocsError(f"unknown job {job_id!r}")
        return Job(
            job_id=job.job_id,
            status=job.status,
            chunk_diffs=list(job.diffs),
            document_html=job.document_html,
            requires_approval=job.status is JobStatus.AWAITING_APPROVAL,
            usage=Usage(
                ops_charged=1,
                was_billable=True,
                bucket_used="promo",
                promotions=[{"ops_remaining": self.ops_budget - self.ops_charged}],
            ),
        )

    def approve(
        self, *, session_id: str, decisions: list[ApprovalDecision], job_id: str
    ) -> ApproveResult:
        job = self.jobs.get(job_id)
        if job is None:
            raise SuperDocsError(f"unknown job {job_id!r}")
        if job.approved:
            # The live API answers this with 400 "Job is not awaiting approval".
            raise SuperDocsError(
                f"job {job_id} is not awaiting approval — it was already decided. A job's "
                f"decisions must be collected and sent in a single call."
            )

        known = {d.change_id for d in job.diffs}
        unknown = [d.change_id for d in decisions if d.change_id not in known]
        if unknown:
            raise SuperDocsError(f"job {job_id} has no changes with ids {unknown}")

        html = job.document_html
        applied = denied = 0
        for decision in decisions:
            diff = next(d for d in job.diffs if d.change_id == decision.change_id)
            if decision.approved:
                html = html.replace(f">{diff.old_html}<", f">{diff.new_html}<")
                applied += 1
            else:
                denied += 1

        job.approved = True
        job.status = JobStatus.COMPLETED
        job.document_html = html
        self.sessions[session_id].html = html
        return ApproveResult(
            status="batch_complete", applied_count=applied, denied_count=denied, job_id=job_id
        )

    def export(
        self, *, session_id: str, fmt: str = "docx", options: ExportOptions | None = None
    ) -> ExportResult:
        session = self.sessions.get(session_id)
        if session is None:
            raise SuperDocsError(
                f"no document loaded in session {session_id!r}. Load a document first, then export."
            )
        name = (options.filename if options and options.filename else session_id) or session_id
        if fmt == "docx":
            return ExportResult(to_docx(session.html, self.images), _DOCX, f"{name}.docx")
        if fmt == "pdf":
            return ExportResult(to_pdf(session.html, self.images), "application/pdf", f"{name}.pdf")
        if fmt in ("html", "markdown", "txt"):
            return ExportResult(session.html.encode(), "text/html", f"{name}.{fmt}")
        raise SuperDocsError(f"unsupported export format {fmt!r}")

    # -------------------------------------------------- images and templates

    def upload_image(
        self, *, data: bytes, filename: str, content_type: str = "image/png"
    ) -> ImageUpload:
        if len(data) > MAX_IMAGE_BYTES:
            raise SuperDocsError(
                f"{filename} is {len(data)} bytes; the image endpoint accepts at most "
                f"{MAX_IMAGE_BYTES}. Resize it before upload."
            )
        if content_type not in ALLOWED_IMAGE_TYPES:
            raise SuperDocsError(
                f"{filename} is {content_type}; accepted types are {sorted(ALLOWED_IMAGE_TYPES)}."
            )
        digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
        ext = {"image/jpeg": "jpg", "image/svg+xml": "svg"}.get(
            content_type, content_type.split("/")[-1]
        )
        url = f"{_IMAGE_HOST}/{digest}.{ext}"
        self.images.by_url[url] = data
        return ImageUpload(url=url, content_type=content_type, size=len(data))

    def upload_template(self, *, data: bytes, filename: str) -> TemplateRef:
        tid = hashlib.sha1(data).hexdigest()[:36]
        ref = TemplateRef(
            id=tid,
            name=filename,
            file_extension=f".{filename.rsplit('.', 1)[-1]}" if "." in filename else "",
            file_size=len(data),
            created_at="2026-08-15T00:00:00+00:00",
        )
        self.templates[tid] = ref
        self.template_bytes[tid] = data
        return ref

    def list_templates(self) -> list[TemplateRef]:
        return sorted(self.templates.values(), key=lambda t: t.name)

    def delete_template(self, template_id: str) -> None:
        if template_id not in self.templates:
            raise SuperDocsError(f"unknown template {template_id!r}")
        del self.templates[template_id]
        self.template_bytes.pop(template_id, None)

    # ------------------------------------------------------------- operational

    def continue_job(self, *, session_id: str, job_id: str, keep_going: bool) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            raise SuperDocsError(f"unknown job {job_id!r}")

    def close(self) -> None:
        return None
