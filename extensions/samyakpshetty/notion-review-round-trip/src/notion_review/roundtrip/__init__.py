"""Round-trip orchestration: the outbound send (Notion → Word) and, later, the inbound apply.

These functions are the only place the Notion and SuperDocs seams meet. Everything below them is
provider-agnostic and driven through the typed client protocols, so the same orchestration runs
against the fakes (keyless tests) and the live services.
"""

from notion_review.roundtrip.outbound import ReviewPacket, reconcile_chunks, send_for_review

__all__ = ["ReviewPacket", "reconcile_chunks", "send_for_review"]
