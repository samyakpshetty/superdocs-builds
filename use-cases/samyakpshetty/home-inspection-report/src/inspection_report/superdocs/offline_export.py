"""Turning report HTML into real .docx and .pdf files, without a network.

This exists so the offline path is genuinely end to end. `make demo` produces files you can
open, and the export verifier reads *those* bytes back — the same verifier, doing the same
checks, that runs against files the live service produced. A fake that returned empty bytes
would make the offline suite prove nothing.

Only the small HTML vocabulary this build emits is handled: h1/h2/h3, p, ul/li, and img.
Anything else is rendered as its text, which is the honest degradation for a stand-in.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Block:
    """One rendered block: a tag, its text, and an image reference if it carries one."""

    tag: str
    text: str = ""
    image_url: str = ""
    image_alt: str = ""


class _Reader(HTMLParser):
    """Flatten report HTML into an ordered list of blocks."""

    BLOCKS = frozenset({"h1", "h2", "h3", "h4", "p", "li", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._open: list[str] = []
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.BLOCKS:
            self._flush()
            self._open.append(tag)
        elif tag == "img":
            a = dict(attrs)
            self.blocks.append(
                Block(tag="img", image_url=a.get("src") or "", image_alt=a.get("alt") or "")
            )

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCKS:
            self._flush()
            if self._open and self._open[-1] == tag:
                self._open.pop()

    def handle_data(self, data: str) -> None:
        if self._open:
            self._buffer.append(data)

    def _flush(self) -> None:
        text = " ".join("".join(self._buffer).split())
        if text and self._open:
            self.blocks.append(Block(tag=self._open[-1], text=text))
        self._buffer.clear()

    def close(self) -> None:
        super().close()
        self._flush()


def read_blocks(html: str) -> list[Block]:
    reader = _Reader()
    reader.feed(html)
    reader.close()
    return reader.blocks


@dataclass
class ImageResolver:
    """Maps an image URL back to its bytes. The fake holds every photo it was given."""

    by_url: dict[str, bytes] = field(default_factory=dict)

    def get(self, url: str) -> bytes | None:
        return self.by_url.get(url.split("?")[0])


_HEADING_SIZE = {"h1": 22, "h2": 16, "h3": 13, "h4": 12}


def _fit(data: bytes, max_w: int, max_h: int) -> bytes:
    """Scale a photograph down to about the size it will be printed at."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        rgb = image.convert("RGB")
        rgb.thumbnail((max_w, max_h))
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=82, optimize=True)
        return buf.getvalue()


def to_docx(html: str, images: ImageResolver) -> bytes:
    """A real Word document, with the photographs embedded as real image parts."""
    from docx import Document
    from docx.shared import Inches

    doc = Document()
    for block in read_blocks(html):
        if block.tag == "img":
            data = images.get(block.image_url)
            if data:
                # As in the PDF path: `alt` is accessibility text, and the format supplies
                # its own visible caption.
                doc.add_picture(io.BytesIO(data), width=Inches(4.5))
            continue
        if block.tag in _HEADING_SIZE:
            doc.add_heading(block.text, level=int(block.tag[1]))
        elif block.tag == "li":
            doc.add_paragraph(block.text, style="List Bullet")
        else:
            doc.add_paragraph(block.text)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def to_pdf(html: str, images: ImageResolver) -> bytes:
    """A real PDF, laid out simply, with the photographs embedded."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    width, height = page.rect.width, page.rect.height
    margin, y = 60.0, 60.0

    def new_page() -> None:
        nonlocal page, y
        page = doc.new_page()
        y = margin

    for block in read_blocks(html):
        if block.tag == "img":
            data = images.get(block.image_url)
            if not data:
                continue
            box_h = 150.0
            if y + box_h > height - margin:
                new_page()
            rect = fitz.Rect(margin, y, margin + 220, y + box_h)
            # Downscale to roughly the printed size first. Inserting a full-resolution
            # photograph stores it uncompressed, which turned an eight-photo report into an
            # 8 MB file for 57 KB of actual image data.
            page.insert_image(rect, stream=_fit(data, 220 * 2, int(box_h) * 2))
            # `alt` is for screen readers, not for drawing: the report format already carries
            # a visible caption paragraph, and painting both prints every caption twice.
            y += box_h + 10
            continue

        size = _HEADING_SIZE.get(block.tag, 10.5)
        font = "hebo" if block.tag in _HEADING_SIZE else "helv"
        # Wrap by character count — crude, but this is a stand-in renderer and the verifier
        # reads text content, not line breaks.
        per_line = max(20, int((width - 2 * margin) / (size * 0.5)))
        line = ""
        for word in block.text.split():
            if len(line) + len(word) + 1 > per_line:
                if y > height - margin:
                    new_page()
                page.insert_text((margin, y), line, fontsize=size, fontname=font)
                y += size + 4
                line = word
            else:
                line = f"{line} {word}".strip()
        if line:
            if y > height - margin:
                new_page()
            page.insert_text((margin, y), line, fontsize=size, fontname=font)
            y += size + 4
        y += 6

    out: bytes = doc.tobytes()
    doc.close()
    return out
