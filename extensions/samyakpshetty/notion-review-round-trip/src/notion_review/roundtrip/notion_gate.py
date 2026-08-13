"""The approval gate, driven from inside Notion.

Approval is an *operation*, not a screen: ``InboundController`` exposes it, and any surface can
drive it. This driver puts that surface where the page owner already works. Each pending change
becomes a row in a review-queue database on the page; the owner sets **Status** to Approved or
Rejected in Notion, and this module reads those decisions, hands them to the controller, and writes
the outcome back onto each row. No second app, no extra login, no hosted frontend.

Decisions are collected by polling — Notion's webhooks are not relied on here — which is fine for a
review that takes hours or days, and keeps the integration to one moving part.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from notion_review.domain import ProposedChange, ReviewRound
from notion_review.logging import get_logger
from notion_review.notion.base import NotionClient, NotionError
from notion_review.notion.models import plain_text
from notion_review.notion.queue_schema import (
    STATUS_PENDING,
    decision_from_status,
    outcome_properties,
    queue_properties,
    row_properties,
)
from notion_review.roundtrip.inbound import plain_text_from_html
from notion_review.store import Store

_log = get_logger("notion_review.notion_gate")


def ensure_queue(round_: ReviewRound, notion: NotionClient) -> str:
    """Create the round's review queue on the page, once; returns the database id."""
    if round_.queue_database_id:
        return round_.queue_database_id
    database_id = notion.create_database(
        parent_page_id=round_.notion_page_id,
        title=f"Review queue · round {round_.id}",
        properties=queue_properties(),
    )
    round_.queue_database_id = database_id
    _log.info("queue_created", extra={"round_id": round_.id, "database_id": database_id})
    try:
        notion.create_comment(
            page_id=round_.notion_page_id,
            rich_text=plain_text(
                f"Review round {round_.id}: changes are waiting for you in the "
                f"“Review queue · round {round_.id}” database on this page. "
                "Set each row's Status to Approved or Rejected."
            ),
        )
    except NotionError as exc:  # the queue still exists; the notice is a courtesy
        _log.warning("queue_notice_failed", extra={"round_id": round_.id, "error": str(exc)})
    return database_id


def publish_pending(round_: ReviewRound, notion: NotionClient, store: Store) -> int:
    """Add a row for every pending change that does not have one yet. Returns rows added.

    The row ids are persisted here rather than left to the caller: they are the only link between
    a proposal and the owner's decision, so losing them would strand the queue.
    """
    database_id = ensure_queue(round_, notion)
    added = 0
    for proposal in round_.pending():
        if proposal.queue_row_id:
            continue  # already queued (idempotent across restarts and re-publishes)
        row = notion.create_row(
            database_id=database_id,
            properties=row_properties(
                proposal,
                before=plain_text_from_html(proposal.old_html),
                after=plain_text_from_html(proposal.new_html),
            ),
        )
        proposal.queue_row_id = row.page_id
        added += 1
    store.save(round_)
    _log.info("queue_published", extra={"round_id": round_.id, "rows": added})
    return added


def read_decisions(round_: ReviewRound, notion: NotionClient) -> list[dict[str, object]]:
    """The decisions the owner has made so far, as the controller's decision payloads."""
    if not round_.queue_database_id:
        return []
    rows = notion.query_database(round_.queue_database_id)
    status_by_row = {row.page_id: row.status for row in rows}
    decisions: list[dict[str, object]] = []
    for proposal in round_.pending():
        if not proposal.queue_row_id:
            continue
        approved = decision_from_status(status_by_row.get(proposal.queue_row_id, STATUS_PENDING))
        if approved is not None:
            decisions.append({"proposal_id": proposal.id, "approved": approved})
    return decisions


def await_decisions(
    round_: ReviewRound,
    notion: NotionClient,
    *,
    poll_interval_s: float = 15.0,
    timeout_s: float = 86_400.0,
    on_wait: Callable[[int, int], None] | None = None,
) -> list[dict[str, object]]:
    """Poll the queue until every pending change has been decided, or the deadline passes.

    Returns whatever decisions exist when it stops, so a partially-reviewed round still applies
    what the owner approved rather than discarding their work.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        decisions = read_decisions(round_, notion)
        outstanding = len(round_.pending()) - len(decisions)
        if outstanding <= 0 or time.monotonic() >= deadline:
            return decisions
        if on_wait is not None:
            on_wait(len(decisions), len(round_.pending()))
        time.sleep(poll_interval_s)


def record_outcomes(
    round_: ReviewRound, notion: NotionClient, decided: list[ProposedChange]
) -> None:
    """Write what became of each decided change back onto its queue row."""
    for proposal in decided:
        if not proposal.queue_row_id:
            continue
        try:
            notion.update_row(
                page_id=proposal.queue_row_id, properties=outcome_properties(proposal)
            )
        except NotionError as exc:
            _log.warning(
                "queue_outcome_failed",
                extra={"round_id": round_.id, "row": proposal.queue_row_id, "error": str(exc)},
            )
