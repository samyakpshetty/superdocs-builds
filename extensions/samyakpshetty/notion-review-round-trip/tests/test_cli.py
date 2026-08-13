from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from notion_review.cli import main
from notion_review.notion import FakeNotionClient
from notion_review.sample import demo_review_docx
from notion_review.superdocs import FakeSuperDocsClient


def test_demo_runs_the_round_trip_end_to_end() -> None:
    result = CliRunner().invoke(main, ["demo"])
    assert result.exit_code == 0, result.output
    # The whole flow is visible and finished cleanly.
    assert "Sending the Notion page out for review" in result.output
    assert "change(s) proposed" in result.output
    assert "Q4" in result.output  # the proposed edit is shown
    assert "Dana Reviewer" in result.output  # attribution shown
    assert "applied" in result.output
    assert "finished: completed" in result.output


def test_demo_shows_each_change_as_a_card() -> None:
    result = CliRunner().invoke(main, ["demo"])
    assert result.exit_code == 0, result.output
    assert "was:" in result.output and "now:" in result.output  # the diff card
    assert "Sam Editor" in result.output  # the comment reviewer


def test_review_drives_every_batch_before_reporting_the_round_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With more edits than one operation covers, the round is proposed batch by batch. The driver
    # must keep gating until nothing is pending — never declare a round finished mid-way.
    from _docx_fixtures import build_docx

    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    notion = FakeNotionClient()
    page_id = notion.new_page("Many edits")
    blocks = [notion.add(page_id, "paragraph", f"Line number {i} stands.") for i in range(3)]
    superdocs = FakeSuperDocsClient()
    monkeypatch.setattr("notion_review.cli.build_clients", lambda config: (notion, superdocs))
    monkeypatch.setenv("SECTIONS_PER_OP", "1")  # force one edit per batch

    paras = "".join(
        f"<w:p><w:r><w:t>Line number {i} </w:t></w:r>"
        f'<w:del w:id="{i * 2}" w:author="Dana Reviewer" w:date="2026-08-13T10:00:00Z">'
        f"<w:r><w:delText>stands.</w:delText></w:r></w:del>"
        f'<w:ins w:id="{i * 2 + 1}" w:author="Dana Reviewer" w:date="2026-08-13T10:00:00Z">'
        f"<w:r><w:t>moved.</w:t></w:r></w:ins></w:p>"
        for i in range(3)
    )
    document = (
        f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{paras}</w:body></w:document>'
    )
    markup = tmp_path / "marked.docx"
    markup.write_bytes(build_docx(document))

    state = str(tmp_path / "state.db")
    sent = CliRunner().invoke(
        main, ["send", "--page-id", page_id, "--out", str(tmp_path / "o.docx"), "--state", state]
    )
    assert sent.exit_code == 0, sent.output
    round_id = re.search(r"round=(\S+)", sent.output).group(1)  # type: ignore[union-attr]

    reviewed = CliRunner().invoke(
        main, ["review", "--round-id", round_id, "--markup", str(markup), "--state", state]
    )
    assert reviewed.exit_code == 0, reviewed.output
    assert "Batch 2" in reviewed.output and "Batch 3" in reviewed.output  # every batch gated
    assert "finished: completed" in reviewed.output
    assert all("moved." in notion.block_text(b) for b in blocks)  # nothing left behind


def test_send_then_review_share_state_across_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # send and review are separate invocations; they share the round via the state file,
    # and (in this test) the same fake clients so the fake session/page persist.
    notion, page_id = FakeNotionClient.build_sample()
    superdocs = FakeSuperDocsClient()
    monkeypatch.setattr("notion_review.cli.build_clients", lambda config: (notion, superdocs))

    state = str(tmp_path / "state.db")
    out_doc = str(tmp_path / "out.docx")
    sent = CliRunner().invoke(
        main, ["send", "--page-id", page_id, "--out", out_doc, "--state", state]
    )
    assert sent.exit_code == 0, sent.output
    match = re.search(r"round=(\S+)", sent.output)
    assert match, sent.output
    round_id = match.group(1)
    assert Path(out_doc).exists()

    markup = tmp_path / "marked.docx"
    markup.write_bytes(demo_review_docx())
    reviewed = CliRunner().invoke(
        main, ["review", "--round-id", round_id, "--markup", str(markup), "--state", state]
    )
    assert reviewed.exit_code == 0, reviewed.output
    assert "finished: completed" in reviewed.output
    assert "Q4" in reviewed.output
