"""Pure Notion-model helpers: rich-text splitting to stay under Notion's per-object limit."""

from __future__ import annotations

from notion_review.notion.models import Annotations, RichText, plain_text, split_rich_text


def test_split_rich_text_respects_the_limit_and_preserves_style_and_link() -> None:
    long = "x" * 4500
    runs = [RichText(text=long, annotations=Annotations(bold=True), href="https://example.com")]

    out = split_rich_text(runs, limit=2000)

    assert [len(r.text) for r in out] == [2000, 2000, 500]
    assert "".join(r.text for r in out) == long  # nothing lost or reordered
    assert all(r.annotations.bold and r.href == "https://example.com" for r in out)


def test_split_rich_text_leaves_short_runs_untouched() -> None:
    runs = plain_text("short enough")
    assert split_rich_text(runs) == runs
