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
