"""Turning report HTML into real .docx and .pdf files, without a network.

This exists so the offline path is genuinely end to end. `make demo` produces files you can
open, and the export verifier reads *those* bytes back — the same verifier, doing the same
checks, that runs against files the live service produced. A fake that returned empty bytes
would make the offline suite prove nothing.

Only the small HTML vocabulary this build emits is handled: h1/h2/h3, p, ul/li, img, and
``<strong>`` / ``<em>`` inside them. Emphasis is carried rather than flattened, because a
report format is a Word document a firm designs — which severity label is bold, which blurb
is italic — and an exporter that threw that away would show the demo a plainer document than
the product actually produces. Anything outside that vocabulary is rendered as its text,
which is the honest degradation for a stand-in.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Run:
    """A span of text with the emphasis and size it was written with."""

    text: str
    bold: bool = False
    italic: bool = False
    size: float | None = None


@dataclass
class Block:
    """One rendered block: a tag, its runs, and an image reference if it carries one."""

    tag: str
    runs: list[Run] = field(default_factory=list)
    align: str = "left"
    image_url: str = ""
    image_alt: str = ""

    @property
    def text(self) -> str:
        return " ".join("".join(r.text for r in self.runs).split())


def _style_of(attrs: list[tuple[str, str | None]]) -> str:
    return (dict(attrs).get("style") or "").lower()


def _size_in(style: str) -> float | None:
    import re

    match = re.search(r"font-size:\s*([\d.]+)pt", style)
    return float(match.group(1)) if match else None


def _align_in(style: str) -> str:
    import re

    match = re.search(r"text-align:\s*(center|right)", style)
    return match.group(1) if match else "left"


class _Reader(HTMLParser):
    """Flatten report HTML into an ordered list of blocks, keeping emphasis and size.

    Emphasis, size and alignment are carried rather than dropped because they are the
    firm's design, written in Word and returned by the service on the same spans.
    """

    BLOCKS = frozenset({"h1", "h2", "h3", "h4", "p", "li", "blockquote"})
    BOLD = frozenset({"strong", "b"})
    ITALIC = frozenset({"em", "i"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._open: list[str] = []
        self._runs: list[Run] = []
        self._align = "left"
        self._bold = 0
        self._italic = 0
        self._sizes: list[float | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.BLOCKS:
            self._flush()
            self._open.append(tag)
            self._align = _align_in(_style_of(attrs))
        elif tag in self.BOLD:
            self._bold += 1
        elif tag in self.ITALIC:
            self._italic += 1
        elif tag == "span":
            self._sizes.append(_size_in(_style_of(attrs)))
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
            self._align = "left"
        elif tag in self.BOLD:
            self._bold = max(0, self._bold - 1)
        elif tag in self.ITALIC:
            self._italic = max(0, self._italic - 1)
        elif tag == "span" and self._sizes:
            self._sizes.pop()

    def handle_data(self, data: str) -> None:
        if self._open and data.strip():
            size = next((s for s in reversed(self._sizes) if s is not None), None)
            self._runs.append(Run(data, bold=self._bold > 0, italic=self._italic > 0, size=size))

    def _flush(self) -> None:
        if self._runs and self._open:
            self.blocks.append(Block(tag=self._open[-1], runs=list(self._runs), align=self._align))
        self._runs.clear()

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


# Sized like a report rather than like a web page: a section heading sits just above the
# body, the way the Word formats set them.
_HEADING_SIZE = {"h1": 14.5, "h2": 12.5, "h3": 11.5, "h4": 11.0}
_BODY_SIZE = 10.0


def _font(*, bold: bool, italic: bool) -> str:
    """The base-14 PDF font for one combination of emphasis."""
    if bold and italic:
        return "hebi"
    if bold:
        return "hebo"
    if italic:
        return "heit"
    return "helv"


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
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt

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
            continue
        paragraph = (
            doc.add_paragraph(style="List Bullet") if block.tag == "li" else doc.add_paragraph()
        )
        if block.align == "center":
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif block.align == "right":
            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for run in block.runs:
            added = paragraph.add_run(run.text)
            added.bold = run.bold
            added.italic = run.italic
            if run.size is not None:
                added.font.size = Pt(run.size)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def to_pdf(html: str, images: ImageResolver) -> bytes:
    """A real PDF, laid out simply, with the photographs embedded."""
    import pymupdf as fitz

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

        heading = block.tag in _HEADING_SIZE
        base = _HEADING_SIZE.get(block.tag, _BODY_SIZE)
        right = width - margin
        indent = margin + 14 if block.tag == "li" else margin

        # Words first, each carrying the font and size it is to be drawn in, so a bold
        # severity label stays bold in the middle of its line and an 18pt letterhead stays
        # 18pt. Widths are measured rather than guessed at from a character count, which is
        # what makes mixed fonts on one line land correctly.
        words: list[tuple[str, str, float]] = []
        for run in block.runs:
            font = _font(bold=run.bold or heading, italic=run.italic)
            size = run.size or base
            words.extend((word, font, size) for word in run.text.split())
        if not words:
            continue

        # Wrapped before anything is drawn, because a centred line cannot be positioned
        # until its full width is known.
        lines: list[list[tuple[str, str, float]]] = [[]]
        used = 0.0
        for word, font, size in words:
            w = fitz.get_text_length(word, fontname=font, fontsize=size)
            space = fitz.get_text_length(" ", fontname=font, fontsize=size)
            if lines[-1] and indent + used + w > right:
                lines.append([])
                used = 0.0
            lines[-1].append((word, font, size))
            used += w + space

        if heading:
            y += 8  # room above a section, the way the Word formats set it
        for i, line in enumerate(lines):
            size = max(s for _, _, s in line)
            leading = size + 4
            if y + leading > height - margin:
                new_page()
            widths = [fitz.get_text_length(w, fontname=f, fontsize=s) for w, f, s in line]
            spaces = [fitz.get_text_length(" ", fontname=f, fontsize=s) for _, f, s in line]
            total = sum(widths) + sum(spaces[:-1])
            if block.align == "center":
                x = margin + (right - margin - total) / 2
            elif block.align == "right":
                x = right - total
            else:
                x = indent
            if block.tag == "li" and i == 0:
                page.insert_text((margin + 2, y), "•", fontsize=size, fontname="helv")
            for (word, font, run_size), w, space in zip(line, widths, spaces, strict=True):
                page.insert_text((x, y), word, fontsize=run_size, fontname=font)
                x += w + space
            y += leading
        y += 4 if heading else 2

    out: bytes = doc.tobytes()
    doc.close()
    return out
