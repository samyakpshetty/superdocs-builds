"""Starting a review from inside Notion, without a terminal.

Sending a page for review was a developer command, which is not a workflow anyone would use. This
makes it a Notion action instead: a **Review requests** database the team keeps in their workspace.
To send a page out, someone adds a row — the page to review, who should review it — and sets
Status to *Requested*. The service picks it up, runs the round-trip, delivers the document, and
writes back the round it created.

It is deliberately the same mechanism as the approval queue: the service already polls Notion for
the owner's decisions, so it polls for their requests too. One pattern, both directions, and the
owner never leaves Notion.

A row is also what makes the whole thing a single button. Notion's button blocks cannot be created
through the public API — they read back as ``"unsupported"`` — but a person can add one to their
page in Notion itself, with the *Add page to* action pointed at this database and the page filled
in. Clicking it writes the row, and everything below follows: the document goes out on that row,
the marked-up copies come back onto it, and the changes appear on the page for approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from notion_review.logging import get_logger
from notion_review.notion.base import NotionClient
from notion_review.notion.models import FileRef, QueueRow

_log = get_logger("notion_review.requests")

STATUS_REQUESTED = "Requested"
STATUS_SENT = "Sent"
STATUS_FAILED = "Failed"

DOCUMENT_PROP = "Document"  # the styled .docx we send out, attached to the row
RETURNED_PROP = "Returned"  # where reviewers put their marked-up copies back
TAKEN_IN_PROP = "Taken in"  # returned files already handed to the round-trip

_PROP_LIMIT = 1900
# A Notion page id is 32 hex characters, with or without dashes — pull it out of a pasted URL.
_PAGE_ID = re.compile(r"([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})", re.I)
_EMAIL = re.compile(r"[^\s,;<>]+@[^\s,;<>]+\.[^\s,;<>]+")


@dataclass
class ReviewRequest:
    """One row of the requests database: a page someone wants sent out for review."""

    row_id: str
    page_id: str
    reviewers: list[str]
    note: str = ""


def request_properties() -> dict[str, Any]:
    """The schema of the requests database — what a person fills in to start a review."""
    return {
        "Page": {"title": {}},
        "Status": {
            "select": {
                "options": [
                    {"name": STATUS_REQUESTED, "color": "yellow"},
                    {"name": STATUS_SENT, "color": "green"},
                    {"name": STATUS_FAILED, "color": "red"},
                ]
            }
        },
        "Page URL": {"url": {}},
        "Reviewers": {"rich_text": {}},
        "Round": {"rich_text": {}},
        "Result": {"rich_text": {}},
        # The document goes out on the row and comes back on the row, so the whole handoff is
        # visible in Notion and the owner never goes looking in a folder on a server.
        DOCUMENT_PROP: {"files": {}},
        RETURNED_PROP: {"files": {}},
        TAKEN_IN_PROP: {"rich_text": {}},
    }


def files_in(properties: dict[str, Any], name: str) -> list[FileRef]:
    """The files attached to one property, as name and (signed, expiring) URL."""
    prop = properties.get(name) or {}
    entries = prop.get("files")
    if not isinstance(entries, list):
        return []
    files: list[FileRef] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # Notion hosts it (``file``) or it was linked from elsewhere (``external``); both read
        # the same way here, and either is a document a reviewer can have put there.
        holder = entry.get("file") or entry.get("external") or {}
        url = holder.get("url", "") if isinstance(holder, dict) else ""
        if url:
            files.append(FileRef(name=str(entry.get("name") or ""), url=str(url)))
    return files


def attachment_properties(*, upload_id: str, filename: str, name: str = DOCUMENT_PROP) -> Any:
    """The payload that attaches an uploaded file to a row's ``files`` property."""
    return {
        name: {
            "type": "files",
            "files": [{"type": "file_upload", "file_upload": {"id": upload_id}, "name": filename}],
        }
    }


def taken_in(properties: dict[str, Any]) -> set[str]:
    """Which returned files this row has already handed over, by name."""
    return {name for name in _text_of(properties, TAKEN_IN_PROP).split("\n") if name}


def taken_in_properties(names: set[str]) -> dict[str, Any]:
    """Record which returned files have been handed over, so a poll does not re-fetch them.

    Kept on the row rather than in memory so a restart does not download every attachment again.
    It is an optimisation, not the correctness boundary: a file that slips through is recognised
    by its content hash further in and costs nothing.
    """
    listed = "\n".join(sorted(names))[:_PROP_LIMIT]
    return {TAKEN_IN_PROP: {"rich_text": [{"type": "text", "text": {"content": listed}}]}}


def create_request_database(notion: NotionClient, *, parent_page_id: str) -> str:
    """Create the requests database in the workspace; returns its id (put it in config)."""
    database = notion.create_database(
        parent_page_id=parent_page_id,
        title="Review requests",
        properties=request_properties(),
    )
    _log.info("requests_database_created", extra={"database_id": database.id})
    return database.id


def _text_of(row: dict[str, Any], name: str) -> str:
    prop = row.get(name) or {}
    runs = prop.get("rich_text") or prop.get("title") or []
    if isinstance(runs, list):
        return "".join(r.get("plain_text", "") for r in runs)
    return ""


def parse_request(row: QueueRow, properties: dict[str, Any]) -> ReviewRequest | None:
    """Read one row into a request, or ``None`` if it does not name a page we can act on."""
    url_prop = properties.get("Page URL") or {}
    haystack = " ".join(
        [
            str(url_prop.get("url") or ""),
            _text_of(properties, "Page"),
            _text_of(properties, "Round"),
        ]
    )
    match = _PAGE_ID.search(haystack)
    if not match:
        return None
    return ReviewRequest(
        row_id=row.page_id,
        page_id=match.group(1),
        reviewers=_EMAIL.findall(_text_of(properties, "Reviewers")),
        note=_text_of(properties, "Result"),
    )


def mark_request(
    notion: NotionClient, *, row_id: str, status: str, round_id: str = "", result: str = ""
) -> None:
    """Write back what happened to a request, so the row tells its own story."""
    properties: dict[str, Any] = {"Status": {"select": {"name": status}}}
    if round_id:
        properties["Round"] = {
            "rich_text": [{"type": "text", "text": {"content": round_id[:_PROP_LIMIT]}}]
        }
    if result:
        properties["Result"] = {
            "rich_text": [{"type": "text", "text": {"content": result[:_PROP_LIMIT]}}]
        }
    notion.update_row(page_id=row_id, properties=properties)
