"""The shape of the in-Notion review queue: a database the page owner approves changes in.

The owner's source of truth is Notion, so the approval belongs there too — not in a separate app
they have to learn and log into. Each proposed change becomes a row carrying everything needed to
decide: who asked for it, the text before and after, whether SuperDocs' AI authored it, and any
link it would introduce. The owner sets **Status** to Approved or Rejected; the integration reads
those decisions and writes the approved ones onto the blocks, then records the outcome on the row.

These helpers build and read Notion's property payloads so the client stays a thin mapping and the
fake and the live service agree on one format.
"""

from __future__ import annotations

from typing import Any

from notion_review.domain import ChangeSource, ProposalStatus, ProposedChange

STATUS_PENDING = "Pending"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"

_KIND = {
    ChangeSource.TRACKED_CHANGE: "Reviewer edit",
    ChangeSource.COMMENT_INTENT: "AI-authored",
    ChangeSource.COMMENT: "Comment",
}
_PROP_LIMIT = 1900  # Notion caps a rich-text property value at 2000 characters


def queue_properties() -> dict[str, Any]:
    """The database schema: what a reviewer-change row looks like in Notion."""
    return {
        "Change": {"title": {}},
        "Status": {
            "select": {
                "options": [
                    {"name": STATUS_PENDING, "color": "yellow"},
                    {"name": STATUS_APPROVED, "color": "green"},
                    {"name": STATUS_REJECTED, "color": "red"},
                ]
            }
        },
        "Reviewer": {"rich_text": {}},
        "Kind": {
            "select": {
                "options": [
                    {"name": "Reviewer edit", "color": "blue"},
                    {"name": "AI-authored", "color": "purple"},
                    {"name": "Comment", "color": "gray"},
                ]
            }
        },
        "Before": {"rich_text": {}},
        "After": {"rich_text": {}},
        "Notes": {"rich_text": {}},
        "Outcome": {"rich_text": {}},
    }


def _text(value: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": value[:_PROP_LIMIT]}}]}


def _title(value: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": value[:_PROP_LIMIT]}}]}


def _select(value: str) -> dict[str, Any]:
    return {"select": {"name": value}}


def row_properties(proposal: ProposedChange, *, before: str, after: str) -> dict[str, Any]:
    """One pending change, rendered as a row the owner can decide on at a glance."""
    notes: list[str] = []
    if proposal.source == ChangeSource.COMMENT_INTENT and proposal.reviewer_comment:
        notes.append(f"Requested: {proposal.reviewer_comment}")
    if proposal.ai_explanation:
        notes.append(f"SuperDocs: {proposal.ai_explanation}")
    if proposal.source == ChangeSource.COMMENT and proposal.reviewer_comment:
        notes.append(proposal.reviewer_comment)
    if proposal.links:
        notes.append("⚠ links: " + ", ".join(proposal.links))

    summary = f"{before[:60]} → {after[:60]}" if after else (proposal.reviewer_comment or "Comment")
    return {
        "Change": _title(summary),
        "Status": _select(STATUS_PENDING),
        "Reviewer": _text(proposal.reviewer_name),
        "Kind": _select(_KIND.get(proposal.source, "Comment")),
        "Before": _text(before),
        "After": _text(after),
        "Notes": _text(" · ".join(notes)),
    }


def outcome_properties(proposal: ProposedChange) -> dict[str, Any]:
    """What became of a decided change, written back onto its row."""
    outcome = {
        ProposalStatus.APPLIED: "✓ Applied to the page",
        ProposalStatus.REJECTED: "✗ Rejected — not applied",
        ProposalStatus.CONFLICT: "⚠ Skipped — the block changed since review",
        ProposalStatus.FAILED: "! Failed",
    }.get(proposal.status, proposal.status.value)
    if proposal.error:
        outcome = f"{outcome}: {proposal.error}"
    return {"Outcome": _text(outcome)}


def decision_from_status(status: str) -> bool | None:
    """Map a row's Status to a decision. ``None`` means the owner has not decided yet."""
    if status == STATUS_APPROVED:
        return True
    if status == STATUS_REJECTED:
        return False
    return None
