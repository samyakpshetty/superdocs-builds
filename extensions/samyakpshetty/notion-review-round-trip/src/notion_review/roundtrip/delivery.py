"""How the review document reaches the reviewers.

The round-trip is only as usable as its first step: a document that never leaves the machine is
not out for review. This is the seam that hands it over, and a folder is the implementation —
which is a real channel, not a placeholder, when that folder is a shared Drive, Dropbox or
SharePoint folder the reviewers already have. They collect the document there and drop the
marked-up copy back into the intake folder, so the whole cycle runs without anyone in the middle.

Delivery is a seam rather than a hard-coded transport because the round-trip does not care how the
document travelled: it carries its own review-round id and is matched however it comes back. An
email adapter belongs here too — but only alongside an email *intake*, since a reviewer who is
emailed a document will reply to it, and a channel that sends without receiving is a promise the
system cannot keep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from notion_review.logging import get_logger

_log = get_logger("notion_review.delivery")


@dataclass
class Deliverable:
    """A review document on its way to the people who will mark it up."""

    filename: str
    content: bytes
    recipients: list[str] = field(default_factory=list)
    subject: str = "Document for review"
    body: str = ""


@runtime_checkable
class Delivery(Protocol):
    """Somewhere a review document can be handed to its reviewers."""

    def deliver(self, item: Deliverable) -> str:
        """Send it; returns a short receipt describing where it went, for the audit trail."""
        ...


class FolderDelivery:
    """Write the document to a folder the reviewers can reach.

    Synced to a shared drive, this is how a team already passes files around: the document appears
    where the reviewers look, and the marked-up copy goes back into the intake folder.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def deliver(self, item: Deliverable) -> str:
        destination = self.directory / item.filename
        destination.write_bytes(item.content)
        _log.info(
            "delivered_to_folder",
            extra={"path": str(destination), "recipients": len(item.recipients)},
        )
        return f"written to {destination}"
