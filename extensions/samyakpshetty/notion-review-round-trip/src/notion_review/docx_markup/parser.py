"""Read a reviewer's marked-up .docx: tracked changes and comments, with attribution.

python-docx does not expose tracked changes, so we go straight to the OOXML. For each
paragraph we walk its runs in document order and reconstruct two versions — the original (what
we sent) and the reviewer's proposed (accept-all) — from ``w:ins`` / ``w:del`` revisions, and we
attach any comments anchored in the paragraph, each carrying its author. This is the assigned
build's core: reading the returned markup faithfully.
"""

from __future__ import annotations

from lxml.etree import _Element

from notion_review.docx_markup.models import DocxComment, DocxMarkup, ParagraphMarkup
from notion_review.docx_markup.security import DocxError, read_docx_parts, secure_fromstring

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCUMENT = "word/document.xml"
_COMMENTS = "word/comments.xml"


def _w(tag: str) -> str:
    return f"{{{_W}}}{tag}"


def _gather_text(el: _Element, local: str) -> str:
    """Concatenate the text of every ``w:<local>`` descendant of ``el``."""
    return "".join(node.text or "" for node in el.iter(_w(local)))


def parse_docx(data: bytes) -> DocxMarkup:
    """Parse marked-up .docx bytes into a :class:`DocxMarkup`."""
    parts = read_docx_parts(data, {_DOCUMENT, _COMMENTS})
    if _DOCUMENT not in parts:
        raise DocxError("missing word/document.xml — not a Word document")

    document = secure_fromstring(parts[_DOCUMENT])
    comments = _parse_comments(parts.get(_COMMENTS))

    paragraphs = [
        _parse_paragraph(p, index, comments) for index, p in enumerate(document.iter(_w("p")))
    ]
    return DocxMarkup(paragraphs=paragraphs)


def _parse_comments(data: bytes | None) -> dict[str, DocxComment]:
    if data is None:
        return {}
    root = secure_fromstring(data)
    result: dict[str, DocxComment] = {}
    for comment in root.iter(_w("comment")):
        comment_id = comment.get(_w("id"))
        if comment_id is None:
            continue
        result[comment_id] = DocxComment(
            comment_id=comment_id,
            author=comment.get(_w("author"), ""),
            text=_gather_text(comment, "t").strip(),
            date=comment.get(_w("date"), ""),
        )
    return result


def _parse_paragraph(
    paragraph: _Element, index: int, comments: dict[str, DocxComment]
) -> ParagraphMarkup:
    original: list[str] = []
    proposed: list[str] = []
    authors: list[str] = []

    for child in paragraph:
        tag = child.tag  # a callable for comment/PI nodes; those match none of the names below
        if tag == _w("ins"):  # inserted text exists only in the proposed version
            proposed.append(_gather_text(child, "t"))
            _add_author(authors, child.get(_w("author")))
        elif tag == _w("del"):  # deleted text existed only in the original
            original.append(_gather_text(child, "delText"))
            _add_author(authors, child.get(_w("author")))
        elif tag == _w("r"):  # an unchanged run: present in both versions
            text = _gather_text(child, "t")
            original.append(text)
            proposed.append(text)

    comment_ids = [ref.get(_w("id")) for ref in paragraph.iter(_w("commentReference"))]
    para_comments = [comments[cid] for cid in comment_ids if cid and cid in comments]

    return ParagraphMarkup(
        index=index,
        original_text="".join(original),
        proposed_text="".join(proposed),
        authors=list(dict.fromkeys(authors)),  # dedupe, keep first-seen order
        comments=para_comments,
    )


def _add_author(authors: list[str], author: str | None) -> None:
    if author:
        authors.append(author)
