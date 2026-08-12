"""The typed domain for a review round.

A *review round* is the durable object at the centre of everything: a Notion page went out
as a Word document, came back marked up, and each reviewer change is now a :class:`ProposedChange`
moving through a human gate on its way back onto a specific Notion block.

The :class:`BlockMap` is the spine of the whole integration — it ties each Notion block to the
SuperDocs chunk that represents it, so an approved change lands on the *exact* block it came from
and nothing else is touched.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class ChangeOperation(StrEnum):
    """How a change edits its block — mirrors SuperDocs' chunk-diff operations."""

    REPLACE = "replace"
    INSERT = "insert"
    INSERT_AFTER = "insert_after"
    DELETE = "delete"


class ChangeSource(StrEnum):
    """Where in the Word markup a change originated."""

    TRACKED_CHANGE = "tracked_change"  # a w:ins / w:del revision
    COMMENT = "comment"  # a reviewer comment interpreted as an edit instruction


class ProposalStatus(StrEnum):
    PENDING = "pending"  # proposed by SuperDocs, awaiting the human gate
    APPROVED = "approved"  # human approved; queued for write-back
    REJECTED = "rejected"  # human rejected; never written
    APPLIED = "applied"  # written back to Notion and verified
    FAILED = "failed"  # write-back failed; round is resumable
    CONFLICT = "conflict"  # target block drifted; needs human resolution (Phase 2)


class RoundStatus(StrEnum):
    CREATED = "created"
    SENT = "sent"  # exported to Word, out with reviewers
    INGESTING = "ingesting"  # parsing returned markup / proposing changes
    AWAITING_APPROVAL = "awaiting_approval"  # at the item-by-item gate
    APPLYING = "applying"  # writing approved changes back to Notion
    COMPLETED = "completed"
    PARKED = "parked"  # budget/quota tripped; resumable
    FAILED = "failed"


class BlockMapEntry(BaseModel):
    """One Notion block ⇄ one SuperDocs chunk. The reversible link for write-back."""

    notion_block_id: str
    notion_page_id: str = ""  # which page in a multi-page packet this block belongs to
    block_type: str  # notion block type: paragraph, heading_1, toggle, callout, table_row...
    anchor: str  # our stable marker embedded in the HTML, survives the SuperDocs round-trip
    chunk_id: str | None = None  # SuperDocs data-chunk-id, filled after upload
    original_html: str = ""
    original_text: str = ""


class ProposedChange(BaseModel):
    """A single reviewer change, gated on its way back onto a Notion block."""

    id: str = Field(default_factory=lambda: _new_id("chg"))
    chunk_id: str
    notion_block_id: str
    notion_page_id: str = ""  # the page this change writes back to (multi-page packets)
    block_type: str = "paragraph"
    job_id: str = ""  # the SuperDocs chat job that proposed this change (needed to approve it)
    operation: ChangeOperation
    old_html: str = ""
    new_html: str = ""
    source: ChangeSource = ChangeSource.TRACKED_CHANGE
    reviewer_name: str = "Unknown reviewer"
    reviewer_comment: str = ""
    status: ProposalStatus = ProposalStatus.PENDING
    error: str | None = None

    def content_key(self) -> str:
        """Stable idempotency key.

        Two proposals with the same target, operation, and resulting content are the *same*
        change: we never re-send one to SuperDocs or re-apply it to Notion. Normalised so
        insignificant whitespace never splits one change into two ops.
        """
        norm = " ".join(self.new_html.split())
        material = f"{self.notion_block_id}|{self.operation}|{norm}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class ReviewRound(BaseModel):
    """The durable centre of the workflow; persisted so a days-long round survives anything."""

    id: str = Field(default_factory=lambda: _new_id("round"))
    notion_page_id: str
    session_id: str = Field(default_factory=lambda: _new_id("sess"))
    status: RoundStatus = RoundStatus.CREATED
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    version: int = 1  # optimistic-concurrency guard: a stale write is rejected, never lost

    sent_version_id: str | None = None  # SuperDocs version exported to Word
    block_map: list[BlockMapEntry] = Field(default_factory=list)
    proposals: list[ProposedChange] = Field(default_factory=list)

    ops_spent: int = 0  # SuperDocs operations consumed by this round
    review_url: str | None = None  # link recorded back on the Notion page
    stage_timings_ms: dict[str, float] = Field(default_factory=dict)  # where the time went

    def touch(self) -> None:
        self.updated_at = _now()

    def cost_summary(self) -> str:
        """A one-line account of what the round spent and where the time went (behaviour 10)."""
        stages = ", ".join(f"{name} {ms:.0f}ms" for name, ms in self.stage_timings_ms.items())
        ops = f"{self.ops_spent} SuperDocs op(s)"
        return f"{ops}; {stages}" if stages else ops

    def block_for_chunk(self, chunk_id: str) -> BlockMapEntry | None:
        return next((b for b in self.block_map if b.chunk_id == chunk_id), None)

    def page_ids(self) -> list[str]:
        """Every distinct page in this review packet, in first-seen order."""
        seen: dict[str, None] = {}
        for entry in self.block_map:
            seen.setdefault(entry.notion_page_id or self.notion_page_id, None)
        return list(seen)

    def pending(self) -> list[ProposedChange]:
        return [p for p in self.proposals if p.status == ProposalStatus.PENDING]

    def approved(self) -> list[ProposedChange]:
        return [p for p in self.proposals if p.status == ProposalStatus.APPROVED]
