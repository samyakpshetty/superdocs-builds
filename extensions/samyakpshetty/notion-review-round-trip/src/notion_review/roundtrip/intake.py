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
from notion_review.notion.base import NotionClient, NotionError
from notion_review.roundtrip.requests import (
    RETURNED_PROP,
    files_in,
    taken_in,
    taken_in_properties,
)

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


class NotionRowIntake:
    """Take returned reviews off the Notion rows they were requested from.

    The other half of :class:`~notion_review.roundtrip.delivery.NotionRowDelivery`, and the reason
    the whole handoff can stay inside Notion: the marked-up copy is dropped onto the same row the
    document went out on, and the round-trip picks it up from there. A row's *Returned* property
    holds many files, so every reviewer's copy can sit side by side on one row.

    Which files have already been handed over is recorded on the row itself rather than in memory,
    so a restart does not re-download every attachment. That is an optimisation and not the
    correctness boundary: a file that slips past it is recognised by its content hash further in
    and costs nothing.
    """

    def __init__(
        self,
        notion: NotionClient,
        *,
        database_id: str,
        property_name: str = RETURNED_PROP,
    ) -> None:
        self._notion = notion
        self._database_id = database_id
        self._property = property_name
        self._rows: dict[str, str] = {}  # filename -> the row it came from, for accept/reject

    def poll(self) -> list[ReturnedReview]:
        try:
            rows = self._notion.query_database(self._database_id)
        except NotionError as exc:
            _log.warning("returned_rows_unreadable", extra={"error": str(exc)})
            return []
        items: list[ReturnedReview] = []
        for row in rows:
            already = taken_in(row.properties)
            for attached in files_in(row.properties, self._property):
                if attached.name in already or not attached.name.lower().endswith(".docx"):
                    continue
                try:
                    content = self._notion.download_file(attached.url)
                except NotionError as exc:
                    _log.warning(
                        "returned_file_unreadable",
                        extra={"row": row.page_id, "file": attached.name, "error": str(exc)},
                    )
                    continue
                self._rows[attached.name] = row.page_id
                items.append(
                    ReturnedReview(
                        filename=attached.name,
                        content=content,
                        source=f"notion row {row.page_id}",
                    )
                )
        return items

    def accept(self, item: ReturnedReview) -> None:
        self._mark(item, note="")

    def reject(self, item: ReturnedReview, reason: str) -> None:
        # The file stays on the row — nothing a reviewer sent is ever removed — and the row says
        # why it could not be used, where the person who asked for the review will see it.
        self._mark(item, note=f"{item.filename}: {reason}")
        _log.warning("intake_rejected", extra={"file": item.filename, "reason": reason})

    def _mark(self, item: ReturnedReview, *, note: str) -> None:
        row_id = self._rows.pop(item.filename, "")
        if not row_id:
            return
        try:
            row = next(
                (r for r in self._notion.query_database(self._database_id) if r.page_id == row_id),
                None,
            )
            if row is None:
                return
            properties = taken_in_properties(taken_in(row.properties) | {item.filename})
            if note:
                properties["Result"] = {
                    "rich_text": [{"type": "text", "text": {"content": note[:1900]}}]
                }
            self._notion.update_row(page_id=row_id, properties=properties)
        except NotionError as exc:
            # Not fatal: the file is simply offered again next poll and recognised by its hash.
            _log.warning("intake_mark_failed", extra={"row": row_id, "error": str(exc)})


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
