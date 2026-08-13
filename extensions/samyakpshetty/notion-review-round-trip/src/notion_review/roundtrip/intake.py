"""Where a returned review lands, and how it gets noticed.

A reviewer finishes in Word and sends the file back. Nobody should then have to save it somewhere
particular and run a command — the arrival *is* the trigger. This is the seam that makes that true:
a channel hands the service ``ReturnedReview`` items, and the service takes it from there. Because
every returned file carries its own round id (see :mod:`notion_review.docx_markup.stamp`), the
channel does not need to know anything about reviews.

A watched folder is the implementation here: it is real, it needs no infrastructure, and it is how
plenty of teams already work (a shared Drive or Dropbox folder syncs into it). An email adapter —
the reviewer simply replies with the attachment — is the same protocol over IMAP or an inbound-mail
webhook, and an HTTP upload endpoint is the same protocol again; neither changes anything below.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from notion_review.logging import get_logger

_log = get_logger("notion_review.intake")

_DOCX_SUFFIXES = frozenset({".docx"})


@dataclass(frozen=True)
class ReturnedReview:
    """A marked-up document that came back, from whatever channel delivered it."""

    filename: str
    content: bytes
    source: str = ""  # where it came from, for the audit trail (a path, a message id, a URL)


@runtime_checkable
class Intake(Protocol):
    """A channel that returned reviews arrive on."""

    def poll(self) -> list[ReturnedReview]:
        """Any reviews that have arrived since the last call."""
        ...

    def accept(self, item: ReturnedReview) -> None:
        """Mark an item handled, so it is never processed twice."""
        ...

    def reject(self, item: ReturnedReview, reason: str) -> None:
        """Set an item aside — it could not be matched to a round, or it failed to parse."""
        ...


class FolderIntake:
    """Watch a directory for returned reviews; move each one aside once handled.

    Files are moved rather than deleted, so nothing a reviewer sent is ever destroyed: handled
    files land in ``processed/`` and unusable ones in ``failed/`` with the reason alongside.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.processed = self.directory / "processed"
        self.failed = self.directory / "failed"
        for path in (self.directory, self.processed, self.failed):
            path.mkdir(parents=True, exist_ok=True)

    def poll(self) -> list[ReturnedReview]:
        items: list[ReturnedReview] = []
        for path in sorted(self.directory.iterdir()):
            if not path.is_file() or path.suffix.lower() not in _DOCX_SUFFIXES:
                continue
            if path.name.startswith("~$"):  # Word's lock file for an open document
                continue
            items.append(
                ReturnedReview(filename=path.name, content=path.read_bytes(), source=str(path))
            )
        return items

    def accept(self, item: ReturnedReview) -> None:
        self._move(item, self.processed)

    def reject(self, item: ReturnedReview, reason: str) -> None:
        destination = self._move(item, self.failed)
        if destination is not None:
            destination.with_suffix(destination.suffix + ".reason.txt").write_text(reason)
        _log.warning("intake_rejected", extra={"file": item.filename, "reason": reason})

    def _move(self, item: ReturnedReview, into: Path) -> Path | None:
        source = Path(item.source) if item.source else self.directory / item.filename
        if not source.exists():
            return None
        destination = into / source.name
        counter = 1
        while destination.exists():  # never overwrite an earlier return
            destination = into / f"{source.stem}({counter}){source.suffix}"
            counter += 1
        shutil.move(str(source), str(destination))
        return destination
