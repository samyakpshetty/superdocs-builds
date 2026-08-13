"""Build the Notion and SuperDocs clients for the configured provider.

``fake`` returns the deterministic in-memory clients (keyless tests, demo); ``live`` returns the
HTTP clients after validating that credentials are present. Everything downstream depends only on
the typed protocols, so the choice is invisible past this seam.
"""

from __future__ import annotations

from notion_review.config import Config
from notion_review.notion.base import NotionClient
from notion_review.notion.fake import FakeNotionClient
from notion_review.roundtrip.delivery import Delivery, FolderDelivery, SmtpDelivery
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
    """Email the review document when a mail server is configured; otherwise write it to a folder.

    Both are real channels — a synced folder reaches people too — and the round-trip does not care
    which was used, because the document carries its own review-round id either way.
    """
    if config.smtp_host and config.smtp_sender:
        return SmtpDelivery(
            host=config.smtp_host,
            port=config.smtp_port,
            username=config.smtp_username,
            password=config.smtp_password,
            sender=config.smtp_sender,
        )
    return FolderDelivery(folder)
