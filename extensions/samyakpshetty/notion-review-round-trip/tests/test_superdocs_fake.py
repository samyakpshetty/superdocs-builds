from __future__ import annotations

import json

from notion_review.domain import ChangeOperation
from notion_review.superdocs import (
    FakeSuperDocsClient,
    JobStatus,
    SuperDocsClient,
    parse_pending_changes,
)
from notion_review.superdocs.instructions import build_instruction, parse_instruction

DOC = "<h1>Title</h1><p>The quick brown fox.</p><p>Second paragraph here.</p>"


def _edit(find: str, replace: str, op: ChangeOperation = ChangeOperation.REPLACE) -> str:
    return build_instruction(
        operation=op, find_text=find, replace_text=replace, reviewer="Dana Reviewer"
    )


def test_fake_satisfies_the_protocol() -> None:
    assert isinstance(FakeSuperDocsClient(), SuperDocsClient)


def test_upload_assigns_a_chunk_id_per_block() -> None:
    client = FakeSuperDocsClient()
    result = client.upload_document(document_html=DOC, session_id="s1")
    assert result.chunks_count == 3
    assert result.html.count("data-chunk-id") == 3


def test_parse_pending_changes_double_decodes_the_string_form() -> None:
    # The real API delivers this as a JSON-encoded STRING — the #1 integrator trap.
    diff = {
        "chunk_id": "c1",
        "operation": "replace",
        "old_html": "<p>a</p>",
        "new_html": "<p>b</p>",
    }
    as_string = parse_pending_changes({"pending_changes": json.dumps([diff])})
    assert len(as_string) == 1
    assert as_string[0].chunk_id == "c1"
    assert as_string[0].new_html == "<p>b</p>"
    # Tolerates the already-decoded and empty forms too.
    assert len(parse_pending_changes({"pending_changes": [diff]})) == 1
    assert parse_pending_changes({}) == []
    assert parse_pending_changes({"pending_changes": ""}) == []


def test_instruction_round_trips() -> None:
    msg = _edit("old passage", "new passage")
    parsed = parse_instruction(msg)
    assert parsed is not None
    assert parsed.operation == ChangeOperation.REPLACE
    assert parsed.find_text == "old passage"
    assert parsed.replace_text == "new passage"
    assert parse_instruction("not our contract") is None


def test_chat_auto_applies_and_returns_a_diff() -> None:
    client = FakeSuperDocsClient()
    client.upload_document(document_html=DOC, session_id="s1")

    job_id = client.chat_async(
        session_id="s1", message=_edit("The quick brown fox.", "The quick red fox.")
    )
    job = client.get_job(job_id)
    # No review mode: the edit auto-applies and the job completes (session is free), and the
    # diff is still returned so the integration can gate it and write it to the host.
    assert job.status == JobStatus.COMPLETED
    assert len(job.chunk_diffs) == 1
    assert client.monthly_used() == 1  # one op charged
    assert "red fox" in client.session_html("s1")
    assert "brown fox" not in client.session_html("s1")


def test_two_edits_in_one_session_do_not_block() -> None:
    # The reason we dropped review mode: back-to-back edits must both go through.
    client = FakeSuperDocsClient()
    client.upload_document(document_html=DOC, session_id="s1")
    client.chat_async(session_id="s1", message=_edit("The quick brown fox.", "The quick red fox."))
    client.chat_async(session_id="s1", message=_edit("Second paragraph here.", "Second para."))
    html = client.session_html("s1")
    assert "red fox" in html and "Second para." in html


def test_no_op_edit_charges_nothing() -> None:
    client = FakeSuperDocsClient()
    client.upload_document(document_html=DOC, session_id="s1")
    # find_text that matches nothing -> no diff, no op charged (mirrors the free-when-nothing rule)
    client.chat_async(session_id="s1", message=_edit("nonexistent passage", "whatever"))
    assert client.monthly_used() == 0
