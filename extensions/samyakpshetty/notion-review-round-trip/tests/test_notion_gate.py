"""The approval gate driven from inside Notion: a queue the page owner decides in."""

from __future__ import annotations

from _docx_fixtures import CHANGE_AUTHOR, reviewed_docx
from notion_review.config import Config
from notion_review.domain import ProposalStatus, RoundStatus
from notion_review.notion import FakeNotionClient
from notion_review.notion.queue_schema import STATUS_APPROVED, STATUS_PENDING, STATUS_REJECTED
from notion_review.roundtrip import InboundController, send_for_review
from notion_review.roundtrip.notion_gate import (
    await_decisions,
    publish_pending,
    read_decisions,
    record_outcomes,
)
from notion_review.store import SQLiteStore
from notion_review.superdocs import FakeSuperDocsClient


def _setup() -> tuple[FakeNotionClient, SQLiteStore, InboundController, str]:
    notion, page_id = FakeNotionClient.build_sample()
    superdocs = FakeSuperDocsClient()
    store = SQLiteStore()
    packet = send_for_review(notion=notion, superdocs=superdocs, store=store, page_id=page_id)
    controller = InboundController(
        notion=notion, superdocs=superdocs, store=store, config=Config.from_env({})
    )
    return notion, store, controller, packet.round.id


def test_pending_changes_become_rows_the_owner_can_decide() -> None:
    notion, store, controller, round_id = _setup()
    gate = controller.start(round_id=round_id, docx_bytes=reviewed_docx())

    added = publish_pending(gate.round, notion, store)

    assert added == len(gate.pending) == 2
    rows = notion.query_database(gate.round.queue_database_id)
    assert len(rows) == 2
    assert all(row.status == STATUS_PENDING for row in rows)  # nothing decided yet
    # Each row carries what the owner needs to decide, without leaving Notion.
    all_props = [notion.row_properties(row.page_id) for row in rows]
    assert all("Before" in p and "After" in p and "Kind" in p for p in all_props)
    assert any(CHANGE_AUTHOR in str(p["Reviewer"]) for p in all_props)  # attribution reaches Notion
    # The page tells the owner where to look.
    assert any("Review queue" in c.plain() for c in notion.comments_for(gate.round.notion_page_id))


def test_publishing_twice_does_not_duplicate_rows() -> None:
    notion, store, controller, round_id = _setup()
    gate = controller.start(round_id=round_id, docx_bytes=reviewed_docx())

    publish_pending(gate.round, notion, store)
    again = publish_pending(gate.round, notion, store)  # e.g. after a restart

    assert again == 0
    assert len(notion.query_database(gate.round.queue_database_id)) == 2


def test_undecided_rows_yield_no_decisions() -> None:
    notion, store, controller, round_id = _setup()
    gate = controller.start(round_id=round_id, docx_bytes=reviewed_docx())
    publish_pending(gate.round, notion, store)

    assert read_decisions(gate.round, notion) == []  # the owner has not acted


def test_owner_decisions_in_notion_drive_the_round_to_completion() -> None:
    notion, store, controller, round_id = _setup()
    gate = controller.start(round_id=round_id, docx_bytes=reviewed_docx())
    publish_pending(gate.round, notion, store)
    aurora = next(e for e in gate.round.block_map if "Aurora ships in Q3" in e.original_text)
    approved_proposal = next(
        p for p in gate.round.pending() if p.notion_block_id == aurora.notion_block_id
    )
    other = next(p for p in gate.round.pending() if p.id != approved_proposal.id)

    # The page owner decides, in Notion.
    notion.set_row_status(approved_proposal.queue_row_id, STATUS_APPROVED)
    notion.set_row_status(other.queue_row_id, STATUS_REJECTED)

    decisions = await_decisions(gate.round, notion, poll_interval_s=0, timeout_s=1)
    assert len(decisions) == 2

    final = controller.submit(round_id=round_id, decisions=decisions)
    record_outcomes(final, notion, final.proposals)

    assert final.status == RoundStatus.COMPLETED
    assert "Q4" in notion.block_text(aurora.notion_block_id)  # the approved change landed
    applied = next(p for p in final.proposals if p.id == approved_proposal.id)
    rejected = next(p for p in final.proposals if p.id == other.id)
    assert applied.status == ProposalStatus.APPLIED
    assert rejected.status == ProposalStatus.REJECTED
    # The outcome is written back onto each row, so the queue shows what happened.
    assert "Applied" in str(notion.row_properties(applied.queue_row_id)["Outcome"])
    assert "Rejected" in str(notion.row_properties(rejected.queue_row_id)["Outcome"])


def test_a_partly_decided_queue_applies_only_what_was_decided() -> None:
    notion, store, controller, round_id = _setup()
    gate = controller.start(round_id=round_id, docx_bytes=reviewed_docx())
    publish_pending(gate.round, notion, store)
    first = gate.round.pending()[0]
    notion.set_row_status(first.queue_row_id, STATUS_APPROVED)

    # Waiting stops at the deadline and returns what exists — the owner's work is not discarded.
    decisions = await_decisions(gate.round, notion, poll_interval_s=0, timeout_s=0)

    assert decisions == [{"proposal_id": first.id, "approved": True}]
