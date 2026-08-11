from __future__ import annotations

from click.testing import CliRunner

from notion_review.cli import main


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
