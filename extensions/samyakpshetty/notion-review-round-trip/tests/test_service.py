"""The unattended service: a returned file arrives and the review runs itself."""

from __future__ import annotations

from pathlib import Path

from _docx_fixtures import reviewed_docx
from notion_review.config import Config
from notion_review.docx_markup.stamp import stamp_round_id
from notion_review.domain import RoundStatus
from notion_review.notion import FakeNotionClient
from notion_review.notion.queue_schema import STATUS_APPROVED
from notion_review.roundtrip import send_for_review
from notion_review.roundtrip.intake import FolderIntake, ReturnedReview
from notion_review.roundtrip.service import ReviewService
from notion_review.store import SQLiteStore
from notion_review.superdocs import FakeSuperDocsClient


def _service(tmp_path: Path) -> tuple[ReviewService, FakeNotionClient, SQLiteStore, str, Path]:
    notion, page_id = FakeNotionClient.build_sample()
    superdocs = FakeSuperDocsClient()
    store = SQLiteStore()
    packet = send_for_review(notion=notion, superdocs=superdocs, store=store, page_id=page_id)
    inbox = tmp_path / "inbox"
    service = ReviewService(
        intake=FolderIntake(inbox),
        notion=notion,
        superdocs=superdocs,
        store=store,
        config=Config.from_env({}),
    )
    return service, notion, store, packet.round.id, inbox


def test_a_returned_file_is_taken_in_and_queued_without_anyone_running_a_command(
    tmp_path: Path,
) -> None:
    service, notion, store, round_id, inbox = _service(tmp_path)
    # The reviewer sends the file back; it simply lands in the folder.
    (inbox / f"review-{round_id}.docx").write_bytes(stamp_round_id(reviewed_docx(), round_id))

    report = service.tick()

    assert report.ingested == [round_id]
    assert report.queued == 2  # both changes are waiting for the owner in Notion
    round_ = store.get(round_id)
    assert round_ is not None and round_.queue_database_id
    assert len(notion.query_database(round_.queue_database_id)) == 2
    # The file is filed away, so a second pass never processes it twice.
    assert not (inbox / f"review-{round_id}.docx").exists()
    assert (inbox / "processed" / f"review-{round_id}.docx").exists()
    assert service.tick().ingested == []


def test_the_owners_notion_decisions_are_applied_on_the_next_pass(tmp_path: Path) -> None:
    service, notion, store, round_id, inbox = _service(tmp_path)
    (inbox / f"review-{round_id}.docx").write_bytes(stamp_round_id(reviewed_docx(), round_id))
    service.tick()

    round_ = store.get(round_id)
    assert round_ is not None
    aurora = next(e for e in round_.block_map if "Aurora ships in Q3" in e.original_text)
    for proposal in round_.pending():  # the owner approves everything, in Notion
        notion.set_row_status(proposal.queue_row_id, STATUS_APPROVED)

    report = service.tick()

    assert report.applied == 2
    assert report.completed == [round_id]
    assert "Q4" in notion.block_text(aurora.notion_block_id)  # it landed on the page
    final = store.get(round_id)
    assert final is not None and final.status == RoundStatus.COMPLETED


def test_a_file_that_names_no_round_is_set_aside_with_the_reason(tmp_path: Path) -> None:
    service, _, _, _, inbox = _service(tmp_path)
    (inbox / "some-random-document.docx").write_bytes(reviewed_docx())

    report = service.tick()

    assert report.ingested == [] and report.rejected == ["some-random-document.docx"]
    assert (inbox / "failed" / "some-random-document.docx").exists()
    reason = (inbox / "failed" / "some-random-document.docx.reason.txt").read_text()
    assert "round id" in reason


def test_a_file_for_an_unknown_round_is_set_aside(tmp_path: Path) -> None:
    service, _, _, _, inbox = _service(tmp_path)
    stranger = "round_ffffffffffff"
    (inbox / f"review-{stranger}.docx").write_bytes(stamp_round_id(reviewed_docx(), stranger))

    report = service.tick()

    assert report.rejected == [f"review-{stranger}.docx"]
    assert (inbox / "failed" / f"review-{stranger}.docx").exists()


def test_something_that_is_not_a_document_is_set_aside_not_crashed_on(tmp_path: Path) -> None:
    service, _, _, round_id, inbox = _service(tmp_path)
    (inbox / f"review-{round_id}.docx").write_bytes(b"this is not a docx at all")

    report = service.tick()  # must not raise

    assert report.rejected == [f"review-{round_id}.docx"]


def test_the_folder_ignores_words_lock_files_and_other_formats(tmp_path: Path) -> None:
    intake = FolderIntake(tmp_path / "inbox")
    (intake.directory / "~$review.docx").write_bytes(b"lock")
    (intake.directory / "notes.txt").write_text("not a review")

    assert intake.poll() == []


def test_filing_a_return_never_overwrites_an_earlier_one(tmp_path: Path) -> None:
    intake = FolderIntake(tmp_path / "inbox")
    for _ in range(2):
        (intake.directory / "review.docx").write_bytes(b"x")
        item = ReturnedReview(
            filename="review.docx",
            content=b"x",
            source=str(intake.directory / "review.docx"),
        )
        intake.accept(item)

    assert {p.name for p in intake.processed.iterdir()} == {"review.docx", "review(1).docx"}
