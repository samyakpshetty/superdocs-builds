"""Starting a review from Notion: a row in the requests database, no terminal involved."""

from __future__ import annotations

from pathlib import Path

from notion_review.config import Config
from notion_review.domain import RoundStatus
from notion_review.notion import FakeNotionClient
from notion_review.roundtrip.delivery import Deliverable, FolderDelivery
from notion_review.roundtrip.intake import FolderIntake
from notion_review.roundtrip.requests import (
    STATUS_FAILED,
    STATUS_REQUESTED,
    STATUS_SENT,
    create_request_database,
    request_properties,
)
from notion_review.roundtrip.service import ReviewService
from notion_review.store import SQLiteStore
from notion_review.superdocs import FakeSuperDocsClient


def _rt(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": value}, "plain_text": value}]}


def _setup(tmp_path: Path) -> tuple[ReviewService, FakeNotionClient, SQLiteStore, str, str, Path]:
    notion, page_id = FakeNotionClient.build_sample()
    superdocs = FakeSuperDocsClient()
    store = SQLiteStore()
    requests_db = create_request_database(notion, parent_page_id=page_id)
    outbox = tmp_path / "outbox"
    service = ReviewService(
        intake=FolderIntake(tmp_path / "inbox"),
        notion=notion,
        superdocs=superdocs,
        store=store,
        config=Config.from_env({}),
        delivery=FolderDelivery(outbox),
        requests_database_id=requests_db,
    )
    return service, notion, store, requests_db, page_id, outbox


def _request_row(notion: FakeNotionClient, db: str, page_id: str, reviewers: str) -> str:
    row = notion.create_row(
        database_id=db,
        properties={
            "Page": {
                "title": [{"type": "text", "text": {"content": page_id}, "plain_text": page_id}]
            },
            "Status": {"select": {"name": STATUS_REQUESTED}},
            "Reviewers": _rt(reviewers),
        },
    )
    return row.page_id


def test_a_row_in_notion_sends_the_page_out_for_review(tmp_path: Path) -> None:
    service, notion, store, db, page_id, outbox = _setup(tmp_path)
    row_id = _request_row(notion, db, page_id, "dana@example.com, marcus@example.com")

    report = service.tick()

    assert len(report.sent) == 1
    round_id = report.sent[0]
    round_ = store.get(round_id)
    assert round_ is not None and round_.status == RoundStatus.SENT
    # The document was produced and delivered, stamped so it can find its way home.
    delivered = outbox / f"review-{round_id}.docx"
    assert delivered.exists()
    from notion_review.docx_markup.stamp import read_round_id

    assert read_round_id(delivered.read_bytes()) == round_id
    # And the row tells its own story.
    props = notion.row_properties(row_id)
    assert props["Status"] == {"select": {"name": STATUS_SENT}}
    assert round_id in str(props["Round"])
    assert "outbox" in str(props["Result"])


def test_a_sent_request_is_never_sent_twice(tmp_path: Path) -> None:
    service, notion, _store, db, page_id, _ = _setup(tmp_path)
    _request_row(notion, db, page_id, "dana@example.com")

    first = service.tick()
    second = service.tick()

    assert len(first.sent) == 1
    assert second.sent == []  # the row is no longer Requested


def test_a_row_naming_no_page_is_marked_failed_with_the_reason(tmp_path: Path) -> None:
    service, notion, _, db, _, _ = _setup(tmp_path)
    row_id = _request_row(notion, db, "not a page id at all", "dana@example.com")

    report = service.tick()

    assert report.sent == []
    props = notion.row_properties(row_id)
    assert props["Status"] == {"select": {"name": STATUS_FAILED}}
    assert "page id" in str(props["Result"])


def test_a_page_that_cannot_be_read_marks_the_row_failed(tmp_path: Path) -> None:
    service, notion, _, db, _, _ = _setup(tmp_path)
    missing = "3bb1ec1a70b08171a9b7edfe7fc0ffa5"  # well-formed, but not in this workspace
    row_id = _request_row(notion, db, missing, "dana@example.com")

    report = service.tick()  # must not raise

    assert report.sent == []
    assert notion.row_properties(row_id)["Status"] == {"select": {"name": STATUS_FAILED}}


def test_without_the_trigger_configured_the_service_ignores_requests(tmp_path: Path) -> None:
    # The Notion trigger is optional; a deployment can still drive everything from `send`.
    notion, page_id = FakeNotionClient.build_sample()
    store = SQLiteStore()
    db = create_request_database(notion, parent_page_id=page_id)
    _request_row(notion, db, page_id, "dana@example.com")
    service = ReviewService(
        intake=FolderIntake(tmp_path / "inbox"),
        notion=notion,
        superdocs=FakeSuperDocsClient(),
        store=store,
        config=Config.from_env({}),
    )

    assert service.tick().sent == []


def test_the_requests_schema_is_what_a_person_fills_in() -> None:
    schema = request_properties()
    assert set(schema) == {"Page", "Status", "Page URL", "Reviewers", "Round", "Result"}
    options = {o["name"] for o in schema["Status"]["select"]["options"]}
    assert options == {STATUS_REQUESTED, STATUS_SENT, STATUS_FAILED}


def test_folder_delivery_writes_the_document(tmp_path: Path) -> None:
    delivery = FolderDelivery(tmp_path / "out")
    receipt = delivery.deliver(Deliverable(filename="a.docx", content=b"bytes"))
    assert (tmp_path / "out" / "a.docx").read_bytes() == b"bytes"
    assert "a.docx" in receipt
