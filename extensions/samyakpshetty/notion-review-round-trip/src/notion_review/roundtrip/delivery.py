"""How the Word file reaches the reviewers.

The round-trip is only as usable as its first step: a document that never leaves the machine is
not out for review. This is the seam that sends it. A folder implementation is the local
stand-in (drop it in a synced Drive folder and it reaches people); an SMTP implementation emails
it as an attachment, which is what a reviewer who lives in Word actually expects.

Delivery is deliberately a seam rather than a hard-coded mailer: the round-trip does not care how
the file travelled, because the file carries its own review-round id and can be matched however it
comes back.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol, runtime_checkable

from notion_review.logging import get_logger

_log = get_logger("notion_review.delivery")

_DOCX_TYPE = ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")


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
    """Somewhere a review document can be sent."""

    def deliver(self, item: Deliverable) -> str:
        """Send it; returns a short receipt describing where it went, for the audit trail."""
        ...


class FolderDelivery:
    """Write the document to a folder — the local stand-in, and a real channel when synced.

    A shared Drive, Dropbox or SharePoint folder that reviewers already have is a legitimate way
    to hand someone a file, and it needs no credentials to demonstrate.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def deliver(self, item: Deliverable) -> str:
        destination = self.directory / item.filename
        destination.write_bytes(item.content)
        _log.info("delivered_to_folder", extra={"path": str(destination)})
        return f"written to {destination}"


class SmtpDelivery:
    """Email the document as an attachment — what a reviewer in Word expects to receive.

    Credentials are configuration, never code. A reply with the marked-up file comes back through
    the intake seam, so the whole cycle can run over email without anyone touching this system.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        use_tls: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender
        self._use_tls = use_tls

    def deliver(self, item: Deliverable) -> str:
        if not item.recipients:
            raise ValueError("cannot email a review with no recipients")
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = ", ".join(item.recipients)
        message["Subject"] = item.subject
        message.set_content(item.body or "The document for review is attached.")
        message.add_attachment(
            item.content, maintype=_DOCX_TYPE[0], subtype=_DOCX_TYPE[1], filename=item.filename
        )
        with smtplib.SMTP(self._host, self._port, timeout=30) as server:
            if self._use_tls:
                server.starttls()
            if self._username:
                server.login(self._username, self._password)
            server.send_message(message)
        _log.info(
            "delivered_by_email",
            extra={"recipients": len(item.recipients), "file": item.filename},
        )
        return f"emailed to {', '.join(item.recipients)}"
