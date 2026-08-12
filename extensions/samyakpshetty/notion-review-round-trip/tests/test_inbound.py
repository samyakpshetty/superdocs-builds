"""The inbound half: match markup → propose → human gate → apply, end to end on fakes."""

from __future__ import annotations

from _docx_fixtures import (
    CHANGE_AUTHOR,
    COMMENT_AUTHOR,
    DUP_MIDDLE,
    DUP_TEXT,
    duplicate_second_edited_docx,
    reviewed_docx,
    unchanged_docx,
)
from notion_review.config import Config
from notion_review.docx_markup import parse_docx
from notion_review.domain import ChangeSource, ProposalStatus, RoundStatus
from notion_review.notion import FakeNotionClient
from notion_review.notion.html import blocks_to_html
from notion_review.notion.models import plain_text
from notion_review.notion.tree import fetch_block_tree
from notion_review.roundtrip import InboundController, match_edits, send_for_review
from notion_review.roundtrip.checkpoint import open_checkpointer
from notion_review.store import SQLiteStore
from notion_review.superdocs import FakeSuperDocsClient


def _outbound(
    notion: FakeNotionClient, page_id: str, superdocs: FakeSuperDocsClient, store: SQLiteStore
):
    return send_for_review(notion=notion, superdocs=superdocs, store=store, page_id=page_id)


def _setup() -> tuple[FakeNotionClient, FakeSuperDocsClient, SQLiteStore, InboundController, str]:
    notion, page_id = FakeNotionClient.build_sample()
    superdocs = FakeSuperDocsClient()
    store = SQLiteStore()
    packet = _outbound(notion, page_id, superdocs, store)
    controller = InboundController(
        notion=notion, superdocs=superdocs, store=store, config=Config.from_env({})
    )
    return notion, superdocs, store, controller, packet.round.id


# -- unit: matching -----------------------------------------------------------
def test_match_edits_ties_changes_to_blocks() -> None:
    notion, page_id = FakeNotionClient.build_sample()
    _, block_map = blocks_to_html(fetch_block_tree(notion, page_id))
    markup = parse_docx(reviewed_docx())
    matched, unmatched = match_edits(markup, block_map)
    assert unmatched == []
    assert len(matched) == 2
    text_change = next(m for m in matched if m.is_text_change)
    assert "Q4" in text_change.proposed_text and text_change.reviewer == CHANGE_AUTHOR
    comment_edit = next(m for m in matched if not m.is_text_change)
    assert comment_edit.comment and comment_edit.reviewer == COMMENT_AUTHOR


def test_edit_to_the_second_of_two_identical_blocks_lands_on_the_second() -> None:
    # A page with two byte-identical paragraphs; the reviewer edits the second. Matching by text
    # alone would collapse both onto the first block — positional matching must not.
    notion = FakeNotionClient()
    page_id = notion.new_page("Duplicates")
    first = notion.add(page_id, "paragraph", DUP_TEXT)
    notion.add(page_id, "paragraph", DUP_MIDDLE)
    second = notion.add(page_id, "paragraph", DUP_TEXT)

    _, block_map = blocks_to_html(fetch_block_tree(notion, page_id))
    markup = parse_docx(duplicate_second_edited_docx())
    matched, unmatched = match_edits(markup, block_map)

    assert unmatched == []
    assert len(matched) == 1
    assert matched[0].notion_block_id == second  # the second copy, never the first
    assert matched[0].notion_block_id != first


def test_more_same_text_changes_than_blocks_are_surfaced_not_guessed() -> None:
    # One block, but two identical-text paragraphs both edited: the extra change has no block to
    # land on and must be surfaced, never applied to a guessed target.
    notion = FakeNotionClient()
    page_id = notion.new_page("Ambiguous")
    notion.add(page_id, "paragraph", DUP_TEXT)  # a single block with this text
    notion.add(page_id, "paragraph", DUP_MIDDLE)
    _, block_map = blocks_to_html(fetch_block_tree(notion, page_id))

    markup = parse_docx(duplicate_second_edited_docx())  # two paragraphs carry DUP_TEXT
    matched, unmatched = match_edits(markup, block_map)

    assert len(matched) == 0  # the sole edit is on the second copy, which has no block
    assert unmatched == [DUP_TEXT]


def test_drift_in_notion_is_a_conflict_not_a_silent_overwrite() -> None:
    notion, _, store, controller, round_id = _setup()
    round0 = store.get(round_id)
    assert round0 is not None
    aurora = next(e for e in round0.block_map if "Aurora ships in Q3" in e.original_text)

    gate = controller.start(round_id=round_id, docx_bytes=reviewed_docx())
    # Someone edits the page in Notion while it is out for review.
    drifted = "Aurora ships in Q3 — but the scope changed materially after this went out."
    notion.update_block(
        aurora.notion_block_id, block_type="paragraph", rich_text=plain_text(drifted)
    )

    decisions = [{"proposal_id": p.id, "approved": True} for p in gate.pending]
    final = controller.submit(round_id=round_id, decisions=decisions)

    text_change = next(p for p in final.proposals if p.source == ChangeSource.TRACKED_CHANGE)
    assert text_change.status == ProposalStatus.CONFLICT
    # The newer Notion edit is preserved; the stale reviewer change never landed.
    assert notion.block_text(aurora.notion_block_id) == drifted
    assert "Q4" not in notion.block_text(aurora.notion_block_id)


def test_reproposing_a_persisted_round_never_respends_ops() -> None:
    notion, superdocs, store, controller, round_id = _setup()
    controller.start(round_id=round_id, docx_bytes=reviewed_docx())
    after_first = superdocs.chat_calls()
    assert after_first >= 1

    # Simulate a crash/restart: a brand-new controller (fresh graph + checkpointer) re-enters the
    # same persisted round. The already-proposed changes must not be sent to SuperDocs again.
    controller2 = InboundController(
        notion=notion, superdocs=superdocs, store=store, config=Config.from_env({})
    )
    gate = controller2.start(round_id=round_id, docx_bytes=reviewed_docx())

    assert superdocs.chat_calls() == after_first  # zero ops re-spent
    assert len(gate.pending) == 2  # the same proposals are still awaiting approval


def test_a_killed_review_resumes_at_the_gate_from_a_durable_checkpoint(tmp_path) -> None:  # type: ignore[no-untyped-def]
    notion, page_id = FakeNotionClient.build_sample()
    superdocs = FakeSuperDocsClient()
    store = SQLiteStore(str(tmp_path / "state.db"))
    _outbound(notion, page_id, superdocs, store)
    round_id = store.list_ids()[0]
    ckpt = str(tmp_path / "graph.ckpt")

    # First run: propose to the gate, then "crash" — drop the controller entirely.
    first = InboundController(
        notion=notion,
        superdocs=superdocs,
        store=store,
        config=Config.from_env({}),
        checkpointer=open_checkpointer(ckpt),
    )
    gate = first.start(round_id=round_id, docx_bytes=reviewed_docx())
    assert len(gate.pending) == 2
    calls = superdocs.chat_calls()

    # Restart: a fresh controller on the same checkpoint file resumes and applies the decisions,
    # without re-running propose (no SuperDocs op is re-spent).
    resumed = InboundController(
        notion=notion,
        superdocs=superdocs,
        store=store,
        config=Config.from_env({}),
        checkpointer=open_checkpointer(ckpt),
    )
    decisions = [{"proposal_id": p.id, "approved": True} for p in gate.pending]
    final = resumed.submit(round_id=round_id, decisions=decisions)

    assert final.status == RoundStatus.COMPLETED
    assert superdocs.chat_calls() == calls
    assert all(p.status == ProposalStatus.APPLIED for p in final.proposals)


# -- the golden round-trip ----------------------------------------------------
def test_full_round_trip_applies_approved_changes_and_preserves_the_rest() -> None:
    notion, _, store, controller, round_id = _setup()
    round0 = store.get(round_id)
    assert round0 is not None
    aurora = next(e for e in round0.block_map if "Aurora ships in Q3" in e.original_text)
    ingestion = next(e for e in round0.block_map if "ingestion service" in e.original_text)
    overview = next(e for e in round0.block_map if e.original_text == "Overview")
    before = {e.notion_block_id: notion.block_text(e.notion_block_id) for e in round0.block_map}

    gate = controller.start(round_id=round_id, docx_bytes=reviewed_docx())
    assert gate.round.status == RoundStatus.AWAITING_APPROVAL
    assert len(gate.pending) == 2

    decisions = [{"proposal_id": p.id, "approved": True} for p in gate.pending]
    final = controller.submit(round_id=round_id, decisions=decisions)

    assert final.status == RoundStatus.COMPLETED
    # The approved text change landed on the exact block.
    assert "Q4" in notion.block_text(aurora.notion_block_id)
    assert "Q3" not in notion.block_text(aurora.notion_block_id)
    # Everything else is byte-for-byte what it was.
    assert notion.block_text(overview.notion_block_id) == before[overview.notion_block_id]
    # Attribution reached Notion, for both the edit and the comment.
    assert any(
        CHANGE_AUTHOR in c.plain() and final.id in c.plain()
        for c in notion.comments_for(aurora.notion_block_id)
    )
    assert any(COMMENT_AUTHOR in c.plain() for c in notion.comments_for(ingestion.notion_block_id))
    assert all(p.status == ProposalStatus.APPLIED for p in final.proposals)


def test_round_reports_what_it_spent_and_where_the_time_went() -> None:
    _, _, _, controller, round_id = _setup()
    gate = controller.start(round_id=round_id, docx_bytes=reviewed_docx())
    decisions = [{"proposal_id": p.id, "approved": True} for p in gate.pending]
    final = controller.submit(round_id=round_id, decisions=decisions)

    assert final.ops_spent == 1  # one text edit is one op; the comment costs nothing
    assert {"propose", "apply"} <= set(final.stage_timings_ms)
    assert all(ms >= 0 for ms in final.stage_timings_ms.values())
    assert "SuperDocs op(s)" in final.cost_summary()


def test_rejected_change_is_never_written() -> None:
    notion, _, store, controller, round_id = _setup()
    round0 = store.get(round_id)
    assert round0 is not None
    aurora = next(e for e in round0.block_map if "Aurora ships in Q3" in e.original_text)

    gate = controller.start(round_id=round_id, docx_bytes=reviewed_docx())
    text_change = next(p for p in gate.pending if p.source == ChangeSource.TRACKED_CHANGE)
    decisions = [{"proposal_id": text_change.id, "approved": False}]
    final = controller.submit(round_id=round_id, decisions=decisions)

    assert "Q3" in notion.block_text(aurora.notion_block_id)  # unchanged
    rejected = next(p for p in final.proposals if p.id == text_change.id)
    assert rejected.status == ProposalStatus.REJECTED


def test_no_changes_completes_without_a_gate() -> None:
    _, _, _, controller, round_id = _setup()
    gate = controller.start(round_id=round_id, docx_bytes=unchanged_docx("Overview"))
    assert gate.pending == []
    assert gate.round.status == RoundStatus.COMPLETED  # nothing to review → done


def test_budget_exhaustion_parks_the_round() -> None:
    notion, page_id = FakeNotionClient.build_sample()
    superdocs = FakeSuperDocsClient(monthly_limit=0)  # any edit trips the quota
    store = SQLiteStore()
    _outbound(notion, page_id, superdocs, store)
    round_id = store.list_ids()[0]
    controller = InboundController(
        notion=notion, superdocs=superdocs, store=store, config=Config.from_env({})
    )

    gate = controller.start(round_id=round_id, docx_bytes=reviewed_docx())
    assert gate.round.status == RoundStatus.PARKED
    assert gate.pending == []  # circuit-breaker stopped before proposing anything


def test_read_back_mismatch_marks_the_change_failed() -> None:
    # A Notion client whose write silently does not land — the read-back must catch it.
    class DroppingNotion(FakeNotionClient):
        def update_block(self, block_id, *, block_type, rich_text):  # type: ignore[no-untyped-def]
            return self.retrieve_block(block_id)  # pretend success, change nothing

    from _docx_fixtures import PARA_ORIGINAL

    notion = DroppingNotion()
    page_id = notion.new_page("Spec")
    notion.add(page_id, "paragraph", PARA_ORIGINAL)
    superdocs = FakeSuperDocsClient()
    store = SQLiteStore()
    _outbound(notion, page_id, superdocs, store)
    round_id = store.list_ids()[0]
    controller = InboundController(
        notion=notion, superdocs=superdocs, store=store, config=Config.from_env({})
    )

    gate = controller.start(round_id=round_id, docx_bytes=reviewed_docx())
    decisions = [{"proposal_id": p.id, "approved": True} for p in gate.pending]
    final = controller.submit(round_id=round_id, decisions=decisions)

    assert final.status == RoundStatus.FAILED
    failed = [p for p in final.proposals if p.status == ProposalStatus.FAILED]
    assert failed and "read-back" in (failed[0].error or "")
