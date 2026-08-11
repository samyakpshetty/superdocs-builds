"""Build genuine .docx bytes with tracked changes and comments for tests.

python-docx cannot author tracked changes, so we assemble valid OOXML by hand. This produces a
real Word file (a proper zip with the standard parts) that a reviewer's marked-up document looks
exactly like — the parser is therefore tested against reality, not a stub.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# The scenario, with texts tests assert on.
HEADING = "Overview"
_HEAD = "Aurora ships in "
_TAIL = " and targets mid-market teams migrating off spreadsheets."
PARA_ORIGINAL = f"{_HEAD}Q3{_TAIL}"
PARA_PROPOSED = f"{_HEAD}Q4{_TAIL}"
COMMENTED_PARA = "The ingestion service runs on a single Postgres instance behind a queue."
COMMENT_TEXT = "Which Postgres version should we standardise on?"
CHANGE_AUTHOR = "Dana Reviewer"
COMMENT_AUTHOR = "Sam Editor"

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/comments.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    Target="comments.xml"/>
</Relationships>"""


def _document_xml() -> str:
    date = "2026-08-12T10:00:00Z"
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}"><w:body>
  <w:p><w:r><w:t>{HEADING}</w:t></w:r></w:p>
  <w:p>
    <w:r><w:t xml:space="preserve">{_HEAD}</w:t></w:r>
    <w:del w:id="1" w:author="{CHANGE_AUTHOR}" w:date="{date}">
      <w:r><w:delText xml:space="preserve">Q3</w:delText></w:r>
    </w:del>
    <w:ins w:id="2" w:author="{CHANGE_AUTHOR}" w:date="{date}">
      <w:r><w:t xml:space="preserve">Q4</w:t></w:r>
    </w:ins>
    <w:r><w:t xml:space="preserve">{_TAIL}</w:t></w:r>
  </w:p>
  <w:p>
    <w:commentRangeStart w:id="0"/>
    <w:r><w:t xml:space="preserve">{COMMENTED_PARA}</w:t></w:r>
    <w:commentRangeEnd w:id="0"/>
    <w:r><w:commentReference w:id="0"/></w:r>
  </w:p>
</w:body></w:document>"""


def _comments_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments xmlns:w="{W}">
  <w:comment w:id="0" w:author="{COMMENT_AUTHOR}" w:date="2026-08-12T11:00:00Z" w:initials="SE">
    <w:p><w:r><w:t>{COMMENT_TEXT}</w:t></w:r></w:p>
  </w:comment>
</w:comments>"""


def build_docx(document_xml: str, comments_xml: str | None = None) -> bytes:
    """Assemble a .docx from the given parts."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        if comments_xml is not None:
            archive.writestr("word/comments.xml", comments_xml)
    return buffer.getvalue()


def reviewed_docx() -> bytes:
    """The standard scenario: one tracked change (Q3→Q4) and one comment."""
    return build_docx(_document_xml(), _comments_xml())


def unchanged_docx(text: str) -> bytes:
    """A .docx with a single, unmodified paragraph — no tracked changes, no comments."""
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}"><w:body>'
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        f"</w:body></w:document>"
    )
    return build_docx(document)


def zip_with_too_many_members() -> bytes:
    """A zip that trips the member-count guard."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for i in range(2100):
            archive.writestr(f"part{i}.xml", b"x")
    return buffer.getvalue()
