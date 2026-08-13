"""Build the Notion and SuperDocs clients for the configured provider.

``fake`` returns the deterministic in-memory clients (keyless tests, demo); ``live`` returns the
HTTP clients after validating that credentials are present. Everything downstream depends only on
the typed protocols, so the choice is invisible past this seam.
"""

from __future__ import annotations

from notion_review.config import Config
from notion_review.notion.base import NotionClient
from notion_review.notion.fake import FakeNotionClient
from notion_review.roundtrip.delivery import Delivery, FolderDelivery
from notion_review.superdocs.base import SuperDocsClient
from notion_review.superdocs.fake import FakeSuperDocsClient


def build_clients(config: Config) -> tuple[NotionClient, SuperDocsClient]:
    """Return (notion, superdocs) clients for ``config.provider``."""
    if config.provider == "live":
        config.require_live()
        from notion_review.notion.live import LiveNotionClient
        from notion_review.superdocs.live import LiveSuperDocsClient

        assert config.notion_token is not None  # guaranteed by require_live
        assert config.superdocs_api_key is not None
        notion: NotionClient = LiveNotionClient(
            token=config.notion_token, version=config.notion_version, config=config
        )
        superdocs: SuperDocsClient = LiveSuperDocsClient(
            api_key=config.superdocs_api_key, base_url=config.superdocs_base_url, config=config
        )
        return notion, superdocs
    return FakeNotionClient(), FakeSuperDocsClient()


def build_delivery(config: Config, folder: str) -> Delivery:
    """Where requested documents are handed to reviewers — a folder they can reach.

    Synced to a shared drive this is a real channel; the round-trip does not depend on which one,
    because the document carries its own review-round id and is matched however it returns.
    """
    return FolderDelivery(folder)
