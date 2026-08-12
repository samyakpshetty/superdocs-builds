"""Typed Notion models — the subset of blocks the review round-trip preserves.

These mirror Notion's own JSON shapes closely enough to convert faithfully in both directions
while staying small: a block carries its rich-text runs, its type, a little type-specific
metadata (a callout's icon/colour, a code block's language), and its children.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Block types the integration understands. Anything else round-trips as an opaque block:
# preserved on the page, shown to the reviewer as plain text, never silently dropped.
TEXTUAL_TYPES = frozenset(
    {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "toggle",
        "callout",
        "quote",
        "code",
    }
)
CONTAINER_TYPES = frozenset({"toggle", "callout", "table", "column_list", "column"})


class Annotations(BaseModel):
    model_config = {"extra": "ignore"}

    bold: bool = False
    italic: bool = False
    strikethrough: bool = False
    underline: bool = False
    code: bool = False
    color: str = "default"


class RichText(BaseModel):
    """One styled run of text — Notion flattens to ``plain_text`` + ``annotations`` + ``href``."""

    text: str
    annotations: Annotations = Field(default_factory=Annotations)
    href: str | None = None


class Block(BaseModel):
    """A Notion block. ``rich_text`` is its editable text; ``children`` its nested blocks."""

    id: str
    type: str
    rich_text: list[RichText] = Field(default_factory=list)
    children: list[Block] = Field(default_factory=list)
    has_children: bool = False
    # Type-specific metadata preserved across the round-trip (callout icon/colour, code
    # language, to_do checked, table width). Kept opaque so new fields survive untouched.
    meta: dict[str, object] = Field(default_factory=dict)

    def plain(self) -> str:
        return "".join(run.text for run in self.rich_text)


class Page(BaseModel):
    id: str
    title: str = ""
    url: str = ""


class Comment(BaseModel):
    id: str
    parent_id: str  # page id or block id the comment hangs on
    rich_text: list[RichText]
    author: str = ""
    created_time: str = ""

    def plain(self) -> str:
        return "".join(run.text for run in self.rich_text)


class ChildrenPage(BaseModel):
    """One page of ``GET /v1/blocks/{id}/children`` results."""

    results: list[Block] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


def rich(text: str, **annotations: bool | str) -> RichText:
    """Build a single styled run."""
    return RichText(text=text, annotations=Annotations(**annotations))


def plain_text(text: str) -> list[RichText]:
    """Build an unstyled single-run rich-text value."""
    return [RichText(text=text)]


# Notion rejects any single rich-text object whose content exceeds 2000 characters.
NOTION_RICH_TEXT_LIMIT = 2000


def split_rich_text(runs: list[RichText], limit: int = NOTION_RICH_TEXT_LIMIT) -> list[RichText]:
    """Split any run longer than ``limit`` into consecutive runs, preserving style and link.

    Notion caps a rich-text object's content length; a long paragraph would otherwise be rejected
    on write-back. Splitting keeps each run within the cap while the concatenated text — and every
    run's annotations and href — is byte-identical to the input.
    """
    out: list[RichText] = []
    for run in runs:
        if len(run.text) <= limit:
            out.append(run)
            continue
        for start in range(0, len(run.text), limit):
            out.append(
                RichText(
                    text=run.text[start : start + limit],
                    annotations=run.annotations,
                    href=run.href,
                )
            )
    return out


# Block references itself via ``children``; finalise the forward reference.
Block.model_rebuild()
