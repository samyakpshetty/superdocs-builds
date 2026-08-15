"""Turning a `.docx` format into the block HTML SuperDocs produces from the same file.

The binder works on HTML, because that is what comes back when a registered format is loaded
from SuperDocs. Two paths need the same HTML without the service:

* the deterministic fake, which backs the entire application with no API key;
* the fallback in :mod:`inspection_report.templates.registry`, so an inspector who has walked
  a property still gets their report when SuperDocs is unreachable.

So this converter has one job, and it is a fidelity job: **produce the same block structure
the live service produces**, not a prettier one. What the service returns for a `.docx` was
read off a real round trip — a flat sequence of ``<h1>`` / ``<p>`` / ``<ul>`` blocks, one per
chunk, with ``<strong>``, ``<em>`` and ``<span>`` inside them and lists kept whole. Anything
richer here would let a format work offline and fail live, which is the failure this build
has already been bitten by three times.
"""

from __future__ import annotations

import html as html_lib
from pathlib import Path
from typing import Any

from docx import Document


def _runs_to_html(paragraph: Any) -> str:
    out: list[str] = []
    for run in paragraph.runs:
        text = html_lib.escape(run.text)
        if not text:
            continue
        if run.bold:
            text = f"<strong>{text}</strong>"
        if run.italic:
            text = f"<em>{text}</em>"
        # Size and colour ride on a span, which is the shape the live service returns them
        # in. A format's letterhead is 18pt for a reason, and dropping it here would make
        # the offline document quietly plainer than the one the service produces.
        style = []
        if run.font.size is not None:
            style.append(f"font-size:{run.font.size.pt:g}pt")
        colour = getattr(run.font.color, "rgb", None) if run.font.color is not None else None
        if colour is not None:
            style.append(f"color:#{colour}")
        if style:
            text = f'<span style="{";".join(style)}">{text}</span>'
        out.append(text)
    return "".join(out)


def _alignment(paragraph: Any) -> str:
    """Paragraph alignment, as the inline style the service emits."""
    name = getattr(paragraph.alignment, "name", None)
    if name == "CENTER":
        return ' style="text-align:center"'
    if name == "RIGHT":
        return ' style="text-align:right"'
    return ""


def _heading_level(paragraph: Any) -> int | None:
    name = (paragraph.style.name or "") if paragraph.style is not None else ""
    if name.startswith("Heading "):
        try:
            return min(6, max(1, int(name.split()[-1])))
        except ValueError:
            return None
    if name == "Title":
        return 1
    return None


def _is_bullet(paragraph: Any) -> bool:
    name = (paragraph.style.name or "") if paragraph.style is not None else ""
    return name.startswith("List Bullet") or name.startswith("List Number")


def to_html(doc: Any) -> str:
    """Convert an open Word document to the flat block HTML the service returns."""
    blocks: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            items = "".join(f"<li>{b}</li>" for b in bullets)
            blocks.append(f"<ul>{items}</ul>")
            bullets.clear()

    for paragraph in doc.paragraphs:
        inner = _runs_to_html(paragraph)
        if not inner.strip():
            # Word documents carry empty paragraphs for spacing; the service drops them
            # rather than emitting empty chunks.
            continue
        if _is_bullet(paragraph):
            bullets.append(inner)
            continue
        flush()
        level = _heading_level(paragraph)
        align = _alignment(paragraph)
        blocks.append(f"<h{level}{align}>{inner}</h{level}>" if level else f"<p{align}>{inner}</p>")
    flush()
    return "\n".join(blocks)


def from_path(path: Path) -> str:
    return to_html(Document(str(path)))


def from_bytes(data: bytes) -> str:
    import io

    return to_html(Document(io.BytesIO(data)))
