"""The Notion client protocol — the exact operations the round-trip needs, and no more.

Method shapes mirror Notion's REST API so the live client is a thin mapping and the fake is a
faithful stand-in: retrieve a page, page through a block's children, update one block's text,
and attach a comment. Write-back is deliberately per-block (``update_block``) — we only ever
touch the blocks a reviewer changed, so everything else provably stays as it was.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from notion_review.notion.models import Block, ChildrenPage, Comment, Page, RichText


class NotionError(Exception):
    """A Notion operation failed (missing object, invalid request, transport error)."""


class NotionNotFoundError(NotionError):
    """The requested page or block does not exist / is not shared with the integration."""


@runtime_checkable
class NotionClient(Protocol):
    def retrieve_page(self, page_id: str) -> Page: ...

    def retrieve_block(self, block_id: str) -> Block:
        """Fetch a single block — used to verify a write-back actually landed."""
        ...

    def list_block_children(
        self, block_id: str, *, start_cursor: str | None = None, page_size: int = 100
    ) -> ChildrenPage:
        """One page of children — the API is paginated at 100, callers must follow the cursor."""
        ...

    def update_block(self, block_id: str, *, block_type: str, rich_text: list[RichText]) -> Block:
        """Replace one block's rich text in place. Only changed blocks are ever passed here."""
        ...

    def create_comment(
        self,
        *,
        rich_text: list[RichText],
        page_id: str | None = None,
        block_id: str | None = None,
    ) -> Comment:
        """Attach a comment to a page or block — how reviewer attribution reaches Notion."""
        ...
