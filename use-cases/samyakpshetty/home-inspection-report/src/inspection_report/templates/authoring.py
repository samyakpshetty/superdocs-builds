"""The shipped report formats, authored as Word documents.

These are the product's answer to the card's premise. The brief says inspection reports are
not written for a buyer who has never read one, so a builder that asks a firm to upload its
existing report and binds data into it would faithfully reproduce the problem it exists to
solve. The formats ship, and a firm chooses among them — or opens one in Word, changes it,
and drops it back in, because it is a Word document and not a markup dialect.

Why the formats are generated from code rather than committed as opaque binaries: a `.docx`
is a zip, so a change to one is unreviewable in a pull request. Generating them keeps the
diff readable, keeps the catalogue as the single source of the systems and the severity
scale, and keeps the three formats honestly consistent with each other. The generated files
are written into ``templates/`` and are what gets registered; nothing stops a firm replacing
them with a Word file of their own, which is the point.

Every format must satisfy :func:`inspection_report.templates.binding.read_format`: a heading
per catalogue system with a ``[findings]`` slot under it, and one worked example showing how
a finding is recorded. The test suite holds all three to exactly that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

from inspection_report.domain import catalogue

# The standing text every format carries. Observational by construction, and covered by the
# language rail's own tests so this wording can never drift into a claim.
PREAMBLE = (
    "This report records what was observed at the property on the date of the inspection. "
    "It is a visual examination of the systems listed below and is not a certification, "
    "warranty or guarantee of the condition of the property. Where an item is recommended "
    "for further evaluation, that means a qualified specialist should look at it before you "
    "rely on it."
)

LIMITS = (
    "Only areas that were visible and safely accessible on the day were examined. Anything "
    "concealed by finishes, stored belongings, insulation or weather was not inspected, and "
    "nothing in this report should be read as a statement about those areas. Conditions at a "
    "property change, and this report describes one day."
)

GROUPING_NOTE = (
    "Findings are grouped by the system they belong to, and within each system the items "
    "that warrant the soonest attention come first. Every entry records what was observed "
    "and what is recommended next."
)

EXAMPLE_HEADING = "How each finding is recorded"
EXAMPLE_NOTE = (
    "This section shows the shape every finding below takes. It is part of the format, not "
    "part of a finished report, and it is removed when a report is produced."
)


def _letterhead(doc: Any, subtitle: str) -> None:
    head = doc.add_paragraph()
    head.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = head.add_run("[firm name]")
    run.bold = True
    run.font.size = Pt(18)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    srun = sub.add_run(subtitle)
    srun.font.size = Pt(11)
    srun.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    rule = doc.add_paragraph()
    rrun = rule.add_run("_" * 66)
    rrun.font.color.rgb = RGBColor(0xC0, 0xC0, 0xC0)
    rrun.font.size = Pt(8)

    for label, token in (
        ("Property", "[property address]"),
        ("Inspected by", "[inspector]"),
        ("Date of inspection", "[date of inspection]"),
    ):
        p = doc.add_paragraph()
        p.add_run(f"{label}: ").bold = True
        p.add_run(token)


def _legend(doc: Any) -> None:
    doc.add_heading("Severity labels used in this report", level=1)
    for sev in catalogue.severities():
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(f"{sev.label} — ").bold = True
        p.add_run(sev.description.strip())


def _worked_example(doc: Any, *, with_photos: bool, with_recommendation: bool = True) -> None:
    doc.add_heading(EXAMPLE_HEADING, level=1)
    doc.add_paragraph(EXAMPLE_NOTE)
    title = doc.add_paragraph()
    title.add_run("[severity label]: [location]").bold = True
    doc.add_paragraph("[observation]")
    if with_recommendation:
        rec = doc.add_paragraph()
        rec.add_run("Recommended next step: ").bold = True
        rec.add_run("[recommendation]")
    if with_photos:
        doc.add_paragraph("[photograph]")
        cap = doc.add_paragraph()
        cap.add_run("[caption]").italic = True


def _system_sections(doc: Any, *, with_blurb: bool = True, with_summary: bool = True) -> None:
    for system in catalogue.systems():
        doc.add_heading(system.name, level=1)
        if with_blurb:
            blurb = doc.add_paragraph()
            blurb.add_run(system.blurb.strip()).italic = True
        if with_summary:
            doc.add_paragraph("[system summary]")
        doc.add_paragraph("[findings]")


def _base(subtitle: str) -> Any:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    _letterhead(doc, subtitle)
    return doc


def buyer_summary() -> Any:
    """The default: written for someone who has never read an inspection report."""
    doc = _base("RESIDENTIAL PROPERTY INSPECTION REPORT")
    doc.add_heading("How to read this report", level=1)
    doc.add_paragraph(PREAMBLE)
    doc.add_paragraph(
        GROUPING_NOTE + " Photographs are included so you can see what is being described."
    )
    _legend(doc)
    _worked_example(doc, with_photos=True)
    doc.add_heading("What was inspected", level=1)
    doc.add_paragraph("[systems inspected]")
    _system_sections(doc)
    doc.add_heading("Scope and limits of this inspection", level=1)
    doc.add_paragraph(LIMITS)
    return doc


def full_technical() -> Any:
    """Everything, for the file: the same findings with the scope text carried up front."""
    doc = _base("FULL TECHNICAL INSPECTION REPORT")
    doc.add_heading("Scope of this inspection", level=1)
    doc.add_paragraph(PREAMBLE)
    doc.add_paragraph(LIMITS)
    doc.add_heading("How to read this report", level=1)
    doc.add_paragraph(GROUPING_NOTE)
    _legend(doc)
    _worked_example(doc, with_photos=True)
    doc.add_heading("Systems examined", level=1)
    doc.add_paragraph("[systems inspected]")
    _system_sections(doc)
    doc.add_heading("Limitations", level=1)
    doc.add_paragraph(LIMITS)
    return doc


def repair_priority() -> Any:
    """A working list for getting quotes: no photographs, no blurbs, the actions only."""
    doc = _base("REPAIR PRIORITY LIST")
    doc.add_heading("What this list is", level=1)
    doc.add_paragraph(PREAMBLE)
    doc.add_paragraph(
        "This list carries the same findings as the full report, ordered so the items that "
        "warrant the soonest attention appear first under each system. It is intended for "
        "taking to contractors for quotes. It carries no photographs; the full report does."
    )
    _legend(doc)
    _worked_example(doc, with_photos=False)
    doc.add_heading("Systems covered", level=1)
    doc.add_paragraph("[systems inspected]")
    _system_sections(doc, with_blurb=False, with_summary=False)
    doc.add_heading("Scope and limits", level=1)
    doc.add_paragraph(LIMITS)
    return doc


FORMATS = {
    "buyer_summary": buyer_summary,
    "full_technical": full_technical,
    "repair_priority": repair_priority,
}


def write_all(directory: Path) -> list[Path]:
    """Author every shipped format into ``directory``. Returns the paths written."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for key, builder in sorted(FORMATS.items()):
        path = directory / f"{key}.docx"
        builder().save(str(path))
        written.append(path)
    return written
