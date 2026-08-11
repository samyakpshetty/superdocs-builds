from __future__ import annotations

from notion_review.notion import FakeNotionClient, fetch_block_tree
from notion_review.notion.html import blocks_to_html, render_rich_text
from notion_review.notion.models import Annotations, Block, RichText


def test_rich_text_preserves_styling_and_escapes() -> None:
    runs = [
        RichText(text="bold ", annotations=Annotations(bold=True)),
        RichText(text="link", href="https://example.com/a?b=1&c=2"),
        RichText(text=" <script>", annotations=Annotations(code=True)),
    ]
    html = render_rich_text(runs)
    assert "<strong>bold </strong>" in html
    assert '<a href="https://example.com/a?b=1&amp;c=2">link</a>' in html
    assert "&lt;script&gt;" in html  # escaped, never raw
    assert "<code>" in html


def test_sample_page_renders_structure_and_maps_every_editable_block() -> None:
    client, page_id = FakeNotionClient.build_sample()
    tree = fetch_block_tree(client, page_id)
    html, block_map = blocks_to_html(tree)

    # Structure preserved with the right containers.
    assert "<h1 " in html
    assert "<details " in html and "<summary " in html  # toggle
    assert '<aside class="callout"' in html
    assert "<table " in html and "<tr " in html

    # One map entry per editable block: heading, paragraph, toggle summary, toggle child,
    # callout, and two table rows.
    assert len(block_map) == 7
    types = sorted(e.block_type for e in block_map)
    assert types == [
        "callout",
        "heading_1",
        "paragraph",
        "paragraph",
        "table_row",
        "table_row",
        "toggle",
    ]
    # Every entry anchors to its Notion block id, and that marker is in the HTML.
    for entry in block_map:
        assert entry.anchor == entry.notion_block_id
        assert f'data-nr-id="{entry.notion_block_id}"' in html


def test_inline_database_is_preserved_but_not_mapped() -> None:
    # A child_database is structure we must keep but never edit as text.
    blocks = [
        Block(id="blk_db", type="child_database", meta={"title": "Roadmap"}),
        Block(id="blk_p", type="paragraph", rich_text=[RichText(text="After the DB.")]),
    ]
    html, block_map = blocks_to_html(blocks)
    assert "Roadmap" in html
    assert 'class="inline-db"' in html
    assert [e.notion_block_id for e in block_map] == ["blk_p"]  # db not mapped
