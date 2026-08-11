from __future__ import annotations

import json

from notion_review.domain import ChangeOperation
from notion_review.superdocs import (
    ApprovalDecision,
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


def test_full_edit_flow_proposes_then_applies_on_approval() -> None:
    client = FakeSuperDocsClient()
    client.upload_document(document_html=DOC, session_id="s1")

    job_id = client.chat_async(
        session_id="s1", message=_edit("The quick brown fox.", "The quick red fox.")
    )
    job = client.get_job(job_id)
    assert job.status == JobStatus.AWAITING_APPROVAL
    assert len(job.chunk_diffs) == 1
    assert client.monthly_used() == 1  # one op charged

    target = job.chunk_diffs[0].chunk_id
    client.approve(session_id="s1", decisions=[ApprovalDecision(chunk_id=target, approved=True)])
    assert "red fox" in client.session_html("s1")
    assert "brown fox" not in client.session_html("s1")


def test_rejection_leaves_the_document_untouched() -> None:
    client = FakeSuperDocsClient()
    client.upload_document(document_html=DOC, session_id="s1")
    job_id = client.chat_async(
        session_id="s1", message=_edit("The quick brown fox.", "The quick red fox.")
    )
    target = client.get_job(job_id).chunk_diffs[0].chunk_id
    result = client.approve(
        session_id="s1", decisions=[ApprovalDecision(chunk_id=target, approved=False)]
    )
    assert result.denied_count == 1
    assert "brown fox" in client.session_html("s1")


def test_no_op_edit_charges_nothing() -> None:
    client = FakeSuperDocsClient()
    client.upload_document(document_html=DOC, session_id="s1")
    # find_text that matches nothing -> no diff, no op charged (mirrors the free-when-nothing rule)
    client.chat_async(session_id="s1", message=_edit("nonexistent passage", "whatever"))
    assert client.monthly_used() == 0
