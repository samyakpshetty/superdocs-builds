"""Inbound logic: match the reviewer's markup to blocks, propose edits, apply approved ones.

These are the provider-driven steps the LangGraph nodes call. Kept as plain functions so they are
easy to test directly and easy to reason about: matching and applying are deterministic; only
proposing spends SuperDocs operations, and it is budget-guarded.
"""

from __future__ import annotations

import time
from html import escape

from lxml import html as lxml_html
from lxml.html import HtmlElement
from pydantic import BaseModel

from notion_review.config import Config
from notion_review.docx_markup import DocxComment, DocxMarkup
from notion_review.domain import (
    BlockMapEntry,
    ChangeOperation,
    ChangeSource,
    ProposalStatus,
    ProposedChange,
    ReviewRound,
    RoundStatus,
)
from notion_review.logging import get_logger
from notion_review.notion.base import NotionClient, NotionError
from notion_review.notion.models import plain_text, splice_plain_edit
from notion_review.superdocs.base import SuperDocsClient, SuperDocsError
from notion_review.superdocs.instructions import build_instruction
from notion_review.superdocs.models import Job, JobStatus

_log = get_logger("notion_review.inbound")

_TERMINAL = frozenset(
    {JobStatus.COMPLETED, JobStatus.AWAITING_APPROVAL, JobStatus.FAILED, JobStatus.CANCELLED}
)


def _norm(text: str) -> str:
    return " ".join(text.split()).strip()


def _clip(text: str, limit: int = 120) -> str:
    """Shorten text for a provenance comment without dropping the sense of the change."""
    text = _norm(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def plain_text_from_html(html: str) -> str:
    if not html.strip():
        return ""
    element: HtmlElement = lxml_html.fromstring(f"<div>{html}</div>")
    return _norm(str(element.text_content()))


class MatchedEdit(BaseModel):
    """One reviewer change tied to the Notion block it belongs on."""

    notion_block_id: str
    notion_page_id: str = ""
    block_type: str
    chunk_id: str | None
    original_text: str
    proposed_text: str
    reviewer: str
    is_text_change: bool
    comment: str = ""


def match_edits(
    markup: DocxMarkup, block_map: list[BlockMapEntry]
) -> tuple[list[MatchedEdit], list[str]]:
    """Match each changed paragraph to its block by the as-sent text.

    Matching is positional within a text: the *n*-th block carrying a given as-sent text pairs
    with the *n*-th same-text paragraph, in document order. A page with two identical paragraphs
    therefore no longer collapses both edits onto the first block — an edit to the second lands
    on the second. When a change cannot be located (unknown text, or more same-text paragraphs
    than blocks), it is surfaced in the unmatched list, never applied to a guessed block.

    Returns the matched edits and the texts of any changes we could not locate.
    """
    # Group block-map entries by as-sent text, preserving document order.
    entries_by_text: dict[str, list[BlockMapEntry]] = {}
    for entry in block_map:
        entries_by_text.setdefault(_norm(entry.original_text), []).append(entry)

    # Occurrence ordinal of every paragraph among all same-text paragraphs, in document order.
    # Counting over *all* paragraphs (not just changed ones) is what lets an edit to the second
    # of two identical blocks resolve to the second block rather than the first.
    occurrence: dict[int, int] = {}
    seen: dict[str, int] = {}
    for para in markup.paragraphs:
        key = _norm(para.original_text)
        occurrence[para.index] = seen.get(key, 0)
        seen[key] = occurrence[para.index] + 1

    matched: list[MatchedEdit] = []
    unmatched: list[str] = []
    for para in markup.changes():
        entries = entries_by_text.get(_norm(para.original_text), [])
        ordinal = occurrence[para.index]
        if ordinal >= len(entries):
            unmatched.append(para.original_text)  # unlocatable or ambiguous — surfaced, not guessed
            continue
        hit = entries[ordinal]
        reviewer = _reviewer_of(para.authors, para.comments)
        matched.append(
            MatchedEdit(
                notion_block_id=hit.notion_block_id,
                notion_page_id=hit.notion_page_id,
                block_type=hit.block_type,
                chunk_id=hit.chunk_id,
                original_text=para.original_text,
                proposed_text=para.proposed_text,
                reviewer=reviewer,
                is_text_change=para.is_text_change,
                comment=" ".join(c.text for c in para.comments),
            )
        )
    return matched, unmatched


def _reviewer_of(authors: list[str], comments: list[DocxComment]) -> str:
    if authors:
        return authors[0]
    if comments and comments[0].author:
        return comments[0].author
    return "Unknown reviewer"


def propose_changes(
    round_: ReviewRound,
    superdocs: SuperDocsClient,
    matched: list[MatchedEdit],
    config: Config,
) -> None:
    """Turn matched edits into gated proposals, spending SuperDocs ops frugally and safely."""
    existing_keys = {p.content_key() for p in round_.proposals}
    for edit in matched:
        if config.sample_size is not None and len(round_.proposals) >= config.sample_size:
            break
        if round_.ops_spent >= config.max_ops_per_round:
            round_.status = RoundStatus.PARKED
            _log.warning("ops_budget_exhausted", extra={"round_id": round_.id})
            break

        if edit.is_text_change:
            # SuperDocs locates the chunk by content and returns its own chunk id, so a
            # pre-mapped chunk id is not required — the returned diff carries the id we approve.
            proposal = _propose_text_change(round_, superdocs, edit, config, existing_keys)
            if round_.status == RoundStatus.PARKED:  # circuit-breaker tripped mid-propose
                break
        elif edit.comment:
            proposal = _comment_proposal(edit)
        else:
            continue

        if proposal is None or proposal.content_key() in existing_keys:
            continue  # nothing proposed, or an identical change already exists (idempotent)
        existing_keys.add(proposal.content_key())
        round_.proposals.append(proposal)


def _propose_text_change(
    round_: ReviewRound,
    superdocs: SuperDocsClient,
    edit: MatchedEdit,
    config: Config,
    existing_keys: set[str],
) -> ProposedChange | None:
    proposal = ProposedChange(
        chunk_id="",
        notion_block_id=edit.notion_block_id,
        notion_page_id=edit.notion_page_id,
        block_type=edit.block_type,
        operation=ChangeOperation.REPLACE,
        old_html=f"<p>{escape(edit.original_text)}</p>",
        new_html=f"<p>{escape(edit.proposed_text)}</p>",
        source=ChangeSource.TRACKED_CHANGE,
        reviewer_name=edit.reviewer,
        reviewer_comment=edit.comment,
    )
    # Idempotency where it costs money: an identical change was already proposed on an earlier
    # (interrupted) run, so skip the SuperDocs call entirely — a re-run re-spends zero ops.
    if proposal.content_key() in existing_keys:
        return None

    # Ask SuperDocs to apply the edit — this exercises its edit engine and keeps its copy of
    # the document in sync. It is best-effort: a reviewer's tracked change already carries the
    # exact result, and that reviewer-authoritative text is what we write back to the host, so a
    # SuperDocs hiccup (a slow/busy session, a quirk in how it reports the change) never loses it.
    instruction = build_instruction(
        operation=ChangeOperation.REPLACE,
        find_text=edit.original_text,
        replace_text=edit.proposed_text,
        reviewer=edit.reviewer,
        comment=edit.comment,
    )
    try:
        proposal.job_id = superdocs.chat_async(session_id=round_.session_id, message=instruction)
        job = _poll_job(superdocs, proposal.job_id, config)
        if job.usage is not None:
            round_.ops_spent += job.usage.ops_charged
            if job.usage.quota_exhausted:
                round_.status = RoundStatus.PARKED
                _log.warning("quota_exhausted", extra={"round_id": round_.id})
    except SuperDocsError as exc:
        _log.warning("superdocs_chat_failed", extra={"round_id": round_.id, "error": str(exc)})

    return proposal


def _comment_proposal(edit: MatchedEdit) -> ProposedChange:
    return ProposedChange(
        chunk_id=edit.chunk_id or "",
        notion_block_id=edit.notion_block_id,
        notion_page_id=edit.notion_page_id,
        block_type=edit.block_type,
        operation=ChangeOperation.REPLACE,
        source=ChangeSource.COMMENT,
        reviewer_name=edit.reviewer,
        reviewer_comment=edit.comment,
    )


def _poll_job(superdocs: SuperDocsClient, job_id: str, config: Config) -> Job:
    """Poll an async job to a terminal state, within the configured time budget."""
    deadline = time.monotonic() + config.superdocs_poll_timeout_s
    while True:
        job = superdocs.get_job(job_id)
        if job.status in _TERMINAL:
            return job
        if time.monotonic() > deadline:
            raise TimeoutError(f"SuperDocs job {job_id} did not finish in time")
        time.sleep(config.superdocs_poll_interval_s)


def apply_decisions(
    round_: ReviewRound,
    decisions: list[dict[str, object]],
    notion: NotionClient,
) -> None:
    """Record the human's item-by-item decisions and write approved changes back to Notion.

    SuperDocs is not consulted here: it already produced (and auto-applied to its own copy) the
    edit during propose. The human gate and the authoritative apply both live in this integration,
    against Notion — the review approves what lands on the page.
    """
    decision_by_id = {str(d["proposal_id"]): d for d in decisions}

    # 1. Record the human's decision on each proposed change.
    for proposal in round_.proposals:
        decision = decision_by_id.get(proposal.id)
        if decision is None:
            continue
        approved = bool(decision.get("approved"))
        proposal.status = ProposalStatus.APPROVED if approved else ProposalStatus.REJECTED

    # 2. Write each approved change back to Notion — idempotent, verified, attributed.
    round_.status = RoundStatus.APPLYING
    for proposal in round_.proposals:
        if proposal.status == ProposalStatus.APPROVED:
            _apply_one(round_, proposal, notion)

    failed = any(p.status == ProposalStatus.FAILED for p in round_.proposals)
    round_.status = RoundStatus.FAILED if failed else RoundStatus.COMPLETED

    _post_page_summaries(round_, notion)


def _post_page_summaries(round_: ReviewRound, notion: NotionClient) -> None:
    """Close the loop on each page in the packet with its own outcome — a durable record."""
    fallback = round_.notion_page_id
    for page_id in round_.page_ids():
        on_page = [p for p in round_.proposals if (p.notion_page_id or fallback) == page_id]
        applied = sum(1 for p in on_page if p.status == ProposalStatus.APPLIED)
        rejected = sum(1 for p in on_page if p.status == ProposalStatus.REJECTED)
        conflicts = sum(1 for p in on_page if p.status == ProposalStatus.CONFLICT)
        if not (applied or rejected or conflicts):
            continue  # nothing happened to this page — leave it alone
        summary = f"Review round {round_.id} complete — {applied} applied, {rejected} rejected"
        if conflicts:
            summary += f", {conflicts} skipped (page changed since review)"
        try:
            notion.create_comment(page_id=page_id, rich_text=plain_text(f"{summary}."))
        except NotionError as exc:
            _log.warning(
                "summary_comment_failed",
                extra={"round_id": round_.id, "page_id": page_id, "error": str(exc)},
            )


def _apply_one(round_: ReviewRound, proposal: ProposedChange, notion: NotionClient) -> None:
    if proposal.status == ProposalStatus.APPLIED:
        return  # idempotent: already written on an earlier (interrupted) run
    try:
        if proposal.source == ChangeSource.TRACKED_CHANGE:
            # Drift guard: the reviewer marked up the text we sent. If the Notion block changed
            # since then (someone edited the page while it was out for review), overwriting would
            # silently discard that newer edit. Surface the conflict instead — never clobber.
            as_sent = plain_text_from_html(proposal.old_html)
            current = notion.retrieve_block(proposal.notion_block_id)
            if _norm(current.plain()) != _norm(as_sent):
                proposal.status = ProposalStatus.CONFLICT
                proposal.error = "block changed in Notion since review; not overwritten"
                _log.warning(
                    "drift_conflict",
                    extra={"round_id": round_.id, "proposal_id": proposal.id},
                )
                return
            new_text = plain_text_from_html(proposal.new_html)
            # Surgical write-back: keep the block's untouched runs (bold, links, colour) exactly as
            # they were, rewriting only the span the reviewer actually changed.
            notion.update_block(
                proposal.notion_block_id,
                block_type=proposal.block_type,
                rich_text=splice_plain_edit(current.rich_text, new_text),
            )
            landed = notion.retrieve_block(proposal.notion_block_id)
            if _norm(landed.plain()) != _norm(new_text):
                proposal.status = ProposalStatus.FAILED
                proposal.error = "read-back mismatch: block did not reflect the change"
                return
            note = (
                f"Applied {proposal.reviewer_name}'s change (review round {round_.id}): "
                f"“{_clip(as_sent)}” → “{_clip(new_text)}”"
            )
        else:
            note = (
                f"{proposal.reviewer_name} (review round {round_.id}): {proposal.reviewer_comment}"
            )
        notion.create_comment(
            block_id=proposal.notion_block_id,
            page_id=proposal.notion_page_id or round_.notion_page_id or None,
            rich_text=plain_text(note),
        )
        proposal.status = ProposalStatus.APPLIED
    except NotionError as exc:
        proposal.status = ProposalStatus.FAILED
        proposal.error = str(exc)
        _log.warning(
            "write_back_failed",
            extra={"round_id": round_.id, "proposal_id": proposal.id, "error": str(exc)},
        )
