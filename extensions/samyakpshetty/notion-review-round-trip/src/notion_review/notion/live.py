"""Live Notion client over HTTP, plus the JSON ↔ model mapping.

A thin mapping onto Notion's REST API: retrieve a page, page through a block's children, update
one block's rich text in place, and attach a comment. A client-side rate limiter keeps us under
Notion's ~3 requests/second, and 429/5xx are retried with backoff. The mapping helpers are pure
functions, tested directly against sample Notion JSON with no network.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from notion_review.config import Config
from notion_review.logging import get_logger
from notion_review.notion.base import NotionError, NotionNotFoundError
from notion_review.notion.models import (
    Annotations,
    Block,
    ChildrenPage,
    Comment,
    Page,
    RichText,
)

_log = get_logger("notion_review.notion.live")
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_API = "https://api.notion.com"


class LiveNotionClient:
    """HTTP implementation of :class:`~notion_review.notion.base.NotionClient`."""

    def __init__(
        self, *, token: str, version: str = "2022-06-28", config: Config | None = None
    ) -> None:
        self._client = httpx.Client(
            base_url=_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0),
        )
        self._min_interval = config.notion_min_interval_s if config else 0.34
        self._max_retries = config.notion_max_retries if config else 5
        self._last_call = 0.0

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        attempt = 0
        while True:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            resp = self._client.request(method, path, json=json, params=params)
            self._last_call = time.monotonic()
            if resp.status_code == 404:
                raise NotionNotFoundError(f"{method} {path} -> 404")
            if resp.status_code in _RETRYABLE and attempt < self._max_retries:
                delay = float(resp.headers.get("Retry-After", 0)) or 0.5 * (2**attempt)
                time.sleep(delay)
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise NotionError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
            return resp

    def retrieve_page(self, page_id: str) -> Page:
        data = self._request("GET", f"/v1/pages/{page_id}").json()
        return Page(id=data["id"], title=page_title(data), url=data.get("url", ""))

    def retrieve_block(self, block_id: str) -> Block:
        return block_from_json(self._request("GET", f"/v1/blocks/{block_id}").json())

    def list_block_children(
        self, block_id: str, *, start_cursor: str | None = None, page_size: int = 100
    ) -> ChildrenPage:
        params: dict[str, Any] = {"page_size": page_size}
        if start_cursor:
            params["start_cursor"] = start_cursor
        data = self._request("GET", f"/v1/blocks/{block_id}/children", params=params).json()
        return ChildrenPage(
            results=[block_from_json(b) for b in data.get("results", [])],
            next_cursor=data.get("next_cursor"),
            has_more=data.get("has_more", False),
        )

    def update_block(self, block_id: str, *, block_type: str, rich_text: list[RichText]) -> Block:
        payload = {block_type: {"rich_text": [rich_to_json(rt) for rt in rich_text]}}
        return block_from_json(
            self._request("PATCH", f"/v1/blocks/{block_id}", json=payload).json()
        )

    def create_comment(
        self,
        *,
        rich_text: list[RichText],
        page_id: str | None = None,
        block_id: str | None = None,
    ) -> Comment:
        payload = {
            "parent": {"block_id": block_id} if block_id else {"page_id": page_id},
            "rich_text": [rich_to_json(rt) for rt in rich_text],
        }
        try:
            data = self._request("POST", "/v1/comments", json=payload).json()
        except NotionError:
            # Block-level comments need a recent API version; fall back to a page comment.
            if block_id and page_id:
                payload["parent"] = {"page_id": page_id}
                data = self._request("POST", "/v1/comments", json=payload).json()
            else:
                raise
        return Comment(
            id=data.get("id", ""),
            parent_id=block_id or page_id or "",
            rich_text=rich_text,
            author=str((data.get("created_by") or {}).get("id", "")),
            created_time=data.get("created_time", ""),
        )

    def close(self) -> None:
        self._client.close()


# -- pure JSON <-> model mapping (unit-tested without a network) ---------------
def rich_from_json(node: dict[str, Any]) -> RichText:
    text = node.get("plain_text")
    if text is None:
        text = (node.get("text") or {}).get("content", "")
    return RichText(
        text=text,
        annotations=Annotations.model_validate(node.get("annotations") or {}),
        href=node.get("href"),
    )


def rich_to_json(run: RichText) -> dict[str, Any]:
    text: dict[str, Any] = {"content": run.text}
    if run.href:
        text["link"] = {"url": run.href}
    return {
        "type": "text",
        "text": text,
        "annotations": run.annotations.model_dump(),
    }


def _rich_list(nodes: Any) -> list[RichText]:
    return [rich_from_json(n) for n in nodes] if isinstance(nodes, list) else []


def block_from_json(data: dict[str, Any]) -> Block:
    block_type = data.get("type", "")
    content = data.get(block_type) or {}
    meta: dict[str, object] = {}
    for key in ("color", "language", "checked"):
        if key in content:
            meta[key] = content[key]
    icon = content.get("icon")
    if isinstance(icon, dict) and icon.get("type") == "emoji":
        meta["icon"] = icon.get("emoji", "")
    if block_type == "table_row":
        cells = content.get("cells") or []
        meta["cells"] = [["".join(r.get("plain_text", "") for r in cell)] for cell in cells]
    return Block(
        id=data.get("id", ""),
        type=block_type,
        rich_text=_rich_list(content.get("rich_text")),
        has_children=data.get("has_children", False),
        meta=meta,
    )


def page_title(data: dict[str, Any]) -> str:
    for prop in (data.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return "".join(r.get("plain_text", "") for r in prop.get("title", []))
    return ""
