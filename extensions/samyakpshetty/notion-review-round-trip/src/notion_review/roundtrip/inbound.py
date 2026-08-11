"""Inbound logic: match the reviewer's markup to blocks, propose edits, apply approved ones.

These are the provider-driven steps the LangGraph nodes call. Kept as plain functions so they are
easy to test directly and easy to reason about: matching and applying are deterministic; only
proposing spends SuperDocs operations, and it is budget-guarded.
"""

from __future__ import annotations

import time

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
from notion_review.notion.models import plain_text
from notion_review.superdocs.base import SuperDocsClient, SuperDocsError
from notion_review.superdocs.instructions import build_instruction
from notion_review.superdocs.models import ApprovalDecision, Job, JobStatus

_log = get_logger("notion_review.inbound")

_TERMINAL = frozenset(
    {JobStatus.COMPLETED, JobStatus.AWAITING_APPROVAL, JobStatus.FAILED, JobStatus.CANCELLED}
)


def _norm(text: str) -> str:
    return " ".join(text.split()).strip()


def plain_text_from_html(html: str) -> str:
    if not html.strip():
        return ""
    element: HtmlElement = lxml_html.fromstring(f"<div>{html}</div>")
    return _norm(str(element.text_content()))


class MatchedEdit(BaseModel):
    """One reviewer change tied to the Notion block it belongs on."""

    notion_block_id: str
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

    Returns the matched edits and the texts of any changes we could not locate — surfaced,
    never silently dropped.
    """
    by_text: dict[str, BlockMapEntry] = {}
    for entry in block_map:
        by_text.setdefault(_norm(entry.original_text), entry)

    matched: list[MatchedEdit] = []
    unmatched: list[str] = []
    for para in markup.changes():
        hit = by_text.get(_norm(para.original_text))
        if hit is None:
            unmatched.append(para.original_text)
            continue
        reviewer = _reviewer_of(para.authors, para.comments)
        matched.append(
            MatchedEdit(
                notion_block_id=hit.notion_block_id,
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
            proposal = _propose_text_change(round_, superdocs, edit, config)
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
) -> ProposedChange | None:
    instruction = build_instruction(
        operation=ChangeOperation.REPLACE,
        find_text=edit.original_text,
        replace_text=edit.proposed_text,
        reviewer=edit.reviewer,
        comment=edit.comment,
    )
    job_id = superdocs.chat_async(session_id=round_.session_id, message=instruction)
    job = _poll_job(superdocs, job_id, config)
    if job.usage is not None:
        round_.ops_spent += job.usage.ops_charged
        if job.usage.quota_exhausted:
            round_.status = RoundStatus.PARKED
            _log.warning("quota_exhausted", extra={"round_id": round_.id})
            return None
    if not job.chunk_diffs:
        return None
    diff = job.chunk_diffs[0]  # the instruction is scoped to exactly one block
    return ProposedChange(
        chunk_id=diff.chunk_id or edit.chunk_id or "",
        notion_block_id=edit.notion_block_id,
        block_type=edit.block_type,
        job_id=job_id,
        operation=ChangeOperation.REPLACE,
        old_html=diff.old_html,
        new_html=diff.new_html,
        source=ChangeSource.TRACKED_CHANGE,
        reviewer_name=edit.reviewer,
        reviewer_comment=edit.comment,
    )


def _comment_proposal(edit: MatchedEdit) -> ProposedChange:
    return ProposedChange(
        chunk_id=edit.chunk_id or "",
        notion_block_id=edit.notion_block_id,
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
    superdocs: SuperDocsClient,
    notion: NotionClient,
) -> None:
    """Record the human's item-by-item decisions and write approved changes back to Notion."""
    decision_by_id = {str(d["proposal_id"]): d for d in decisions}

    # 1. Record decisions, and tell SuperDocs about the ones it proposed, grouped by the
    #    chat job that produced them (the approve call is scoped to a job id).
    by_job: dict[str, list[ApprovalDecision]] = {}
    for proposal in round_.proposals:
        decision = decision_by_id.get(proposal.id)
        if decision is None:
            continue
        approved = bool(decision.get("approved"))
        proposal.status = ProposalStatus.APPROVED if approved else ProposalStatus.REJECTED
        if proposal.source == ChangeSource.TRACKED_CHANGE:
            by_job.setdefault(proposal.job_id, []).append(
                ApprovalDecision(
                    chunk_id=proposal.chunk_id,
                    approved=approved,
                    feedback=str(decision.get("feedback", "")),
                )
            )
    for job_id, job_decisions in by_job.items():
        # SuperDocs' approve only syncs its own copy of the document; the authoritative apply
        # is the write-back to Notion below. Degrade gracefully so a SuperDocs-side failure
        # never blocks the host update — the reviewed change still lands where it must.
        try:
            superdocs.approve(session_id=round_.session_id, job_id=job_id, decisions=job_decisions)
        except SuperDocsError as exc:
            _log.warning(
                "superdocs_approve_failed",
                extra={"round_id": round_.id, "job_id": job_id, "error": str(exc)},
            )

    # 2. Write each approved change back to Notion — idempotent, verified, attributed.
    round_.status = RoundStatus.APPLYING
    for proposal in round_.proposals:
        if proposal.status == ProposalStatus.APPROVED:
            _apply_one(round_, proposal, notion)

    failed = any(p.status == ProposalStatus.FAILED for p in round_.proposals)
    round_.status = RoundStatus.FAILED if failed else RoundStatus.COMPLETED

    # Close the loop on the page: a durable record of the review round and its outcome.
    applied = sum(1 for p in round_.proposals if p.status == ProposalStatus.APPLIED)
    rejected = sum(1 for p in round_.proposals if p.status == ProposalStatus.REJECTED)
    try:
        notion.create_comment(
            page_id=round_.notion_page_id,
            rich_text=plain_text(
                f"Review round {round_.id} complete — {applied} applied, {rejected} rejected."
            ),
        )
    except NotionError as exc:
        _log.warning("summary_comment_failed", extra={"round_id": round_.id, "error": str(exc)})


def _apply_one(round_: ReviewRound, proposal: ProposedChange, notion: NotionClient) -> None:
    if proposal.status == ProposalStatus.APPLIED:
        return  # idempotent: already written on an earlier (interrupted) run
    try:
        if proposal.source == ChangeSource.TRACKED_CHANGE:
            new_text = plain_text_from_html(proposal.new_html)
            notion.update_block(
                proposal.notion_block_id,
                block_type=proposal.block_type,
                rich_text=plain_text(new_text),
            )
            landed = notion.retrieve_block(proposal.notion_block_id)
            if _norm(landed.plain()) != _norm(new_text):
                proposal.status = ProposalStatus.FAILED
                proposal.error = "read-back mismatch: block did not reflect the change"
                return
            note = f"Applied {proposal.reviewer_name}'s change from review round {round_.id}."
        else:
            note = (
                f"{proposal.reviewer_name} (review round {round_.id}): {proposal.reviewer_comment}"
            )
        notion.create_comment(block_id=proposal.notion_block_id, rich_text=plain_text(note))
        proposal.status = ProposalStatus.APPLIED
    except NotionError as exc:
        proposal.status = ProposalStatus.FAILED
        proposal.error = str(exc)
        _log.warning(
            "write_back_failed",
            extra={"round_id": round_.id, "proposal_id": proposal.id, "error": str(exc)},
        )
