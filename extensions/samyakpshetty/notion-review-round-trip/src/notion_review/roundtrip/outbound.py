"""Outbound: send a Notion page out for review as a styled Word document.

Steps: fetch the page's block tree → render it to HTML with a reversible block map → upload to
SuperDocs → reconcile SuperDocs' chunk ids back onto the block map → export a styled .docx for
the reviewer → record the durable review round and link it on the Notion page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from lxml import html as lxml_html
from lxml.html import HtmlElement

from notion_review.domain import BlockMapEntry, ReviewRound, RoundStatus
from notion_review.logging import get_logger
from notion_review.notion.base import NotionClient
from notion_review.notion.html import blocks_to_html
from notion_review.notion.models import plain_text
from notion_review.notion.tree import fetch_block_tree
from notion_review.store import Store
from notion_review.superdocs.base import SuperDocsClient
from notion_review.superdocs.models import ExportResult

_log = get_logger("notion_review.outbound")


@dataclass
class ReviewPacket:
    """What a send produces: the persisted round and the reviewer's Word file."""

    round: ReviewRound
    docx: ExportResult


def reconcile_chunks(returned_html: str, block_map: list[BlockMapEntry]) -> int:
    """Fill each block-map entry's ``chunk_id`` from SuperDocs' returned HTML.

    This is best-effort provenance, not a correctness requirement: an edit is proposed by
    content, and SuperDocs returns the chunk id we approve with. We match first on our
    ``data-nr-id`` marker (the fake preserves it) and then, because the live service strips
    unknown attributes and re-chunks its own way, fall back to matching by block text.
    Returns the number of entries matched.
    """
    root = lxml_html.fromstring(f"<div>{returned_html}</div>")
    chunks: list[tuple[str, str | None, str]] = []  # (chunk_id, marker, normalized_text)
    for el in root.iter():
        chunk_id = el.get("data-chunk-id")
        if chunk_id:
            text = " ".join(str(cast(HtmlElement, el).text_content()).split())
            chunks.append((chunk_id, el.get("data-nr-id"), text))

    by_marker = {marker: cid for cid, marker, _ in chunks if marker}
    used: set[str] = set()
    matched = 0

    for entry in block_map:  # 1. exact marker match
        chunk_id = by_marker.get(entry.notion_block_id)
        if chunk_id:
            entry.chunk_id = chunk_id
            used.add(chunk_id)
            matched += 1

    for entry in block_map:  # 2. text match for the rest (marker-stripping / re-chunking)
        if entry.chunk_id:
            continue
        target = " ".join(entry.original_text.split())
        if not target:
            continue
        for chunk_id, _, text in chunks:
            if chunk_id not in used and (target == text or target in text):
                entry.chunk_id = chunk_id
                used.add(chunk_id)
                matched += 1
                break
    return matched


def send_for_review(
    *,
    notion: NotionClient,
    superdocs: SuperDocsClient,
    store: Store,
    page_id: str,
) -> ReviewPacket:
    """Send a Notion page out for formal review; returns the round and the Word file."""
    page = notion.retrieve_page(page_id)
    tree = fetch_block_tree(notion, page_id)
    html, block_map = blocks_to_html(tree)

    round_ = ReviewRound(notion_page_id=page_id, block_map=block_map)
    _log.info("review_round_created", extra={"round_id": round_.id, "blocks": len(block_map)})

    upload = superdocs.upload_document(document_html=html, session_id=round_.session_id)
    matched = reconcile_chunks(upload.html, round_.block_map)
    round_.sent_version_id = upload.version_id
    if matched < len(block_map):
        _log.warning(
            "unmapped_blocks",
            extra={"round_id": round_.id, "matched": matched, "total": len(block_map)},
        )

    docx = superdocs.export(session_id=round_.session_id, fmt="docx")

    round_.review_url = f"{page.url}#review-{round_.id}"
    round_.status = RoundStatus.SENT
    store.save(round_)

    notion.create_comment(
        page_id=page_id,
        rich_text=plain_text(
            f"Sent for review — round {round_.id}. Changes will return here for your approval."
        ),
    )
    _log.info(
        "review_round_sent",
        extra={
            "round_id": round_.id,
            "chunks": upload.chunks_count,
            "docx_bytes": len(docx.content),
        },
    )
    return ReviewPacket(round=round_, docx=docx)
