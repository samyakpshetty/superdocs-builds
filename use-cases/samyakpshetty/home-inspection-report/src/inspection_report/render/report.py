"""Assembling an inspection into report HTML.

This module owns report structure, and it is deliberately the only thing that does. Which
system a finding appears under, the order the systems come in, and the order of findings
within a system are all decided here, from the catalogue and the severity ranks — never by a
model, and never by whatever order the inspector happened to walk the property in.

That is what makes the card's requirement checkable: the export verifier reads the finished
file back and holds it to exactly the structure this module produced.
"""

from __future__ import annotations

import html as html_escape

from inspection_report.domain import catalogue
from inspection_report.domain.models import Finding, Inspection
from inspection_report.templates import binding

# The standing text every report carries. Observational by construction, and covered by the
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

NOTHING_OBSERVED = "No deficiencies were observed in the areas inspected."


def escape(value: str) -> str:
    """Everything a person typed is escaped before it becomes markup.

    An inspector's note is text. If it contains angle brackets, they are angle brackets in
    the report, not tags in the document.
    """
    return html_escape.escape(value, quote=True)


def _finding_title(finding: Finding) -> str:
    if finding.location.strip():
        return escape(finding.location.strip())
    return escape(catalogue.system(finding.system_key).name)


def _sorted_findings(inspection: Inspection, system_key: str) -> list[Finding]:
    """Most urgent first, then stable by the order they were recorded."""
    ranks = {s.key: s.rank for s in catalogue.severities()}
    findings = inspection.findings_for(system_key)
    order = {f.id: i for i, f in enumerate(inspection.findings)}
    return sorted(findings, key=lambda f: (ranks.get(f.severity_key, 999), order[f.id]))


def _photo_rows(finding: Finding) -> list[dict[str, str]]:
    rows = []
    for photo in finding.photos:
        if not photo.uploaded:
            # A photo that never reached the service is left out rather than rendered as a
            # broken image in a document someone is going to print.
            continue
        caption = (
            photo.caption.strip()
            or f"{catalogue.system(finding.system_key).name} — {finding.location or 'photograph'}"
        )
        rows.append({"photo_url": photo.remote_url, "photo_caption": escape(caption)})
    return rows


def _system_summary(count: int, name: str) -> str:
    if count == 0:
        return NOTHING_OBSERVED
    if count == 1:
        return f"One item is recorded under {name}."
    return f"{count} items are recorded under {name}, most urgent first."


def render(inspection: Inspection, template_html: str) -> str:
    """Bind an inspection into a report format. Deterministic: same input, same bytes."""
    system_region = binding.find_region(template_html, "system")
    finding_region = system_region.children().get("finding")
    if finding_region is None:
        raise binding.TemplateError(
            "the system region contains no <!-- region:finding --> block, so findings would "
            "have nowhere to go. Add one inside the system region."
        )
    photo_region = finding_region.children().get("photo")

    system_rows: list[str] = []
    for system in catalogue.systems():
        findings = _sorted_findings(inspection, system.key)
        rendered_findings: list[str] = []
        for finding in findings:
            severity = catalogue.severity(finding.severity_key)
            photos_html = ""
            if photo_region is not None:
                photos_html = binding.render_region(
                    photo_region, _photo_rows(finding), where=f"system:{system.key}"
                )
            body = (
                binding.replace_region(finding_region.body, "photo", photos_html)
                if photo_region
                else finding_region.body
            )
            rendered_findings.append(
                binding.fill(
                    body,
                    {
                        "severity_label": escape(severity.label),
                        "finding_title": _finding_title(finding),
                        "finding_text": escape(finding.prose()),
                        "finding_recommendation": escape(
                            finding.recommendation.strip() or severity.description
                        ),
                    },
                    where=f"system:{system.key}",
                )
            )

        body = binding.replace_region(system_region.body, "finding", "".join(rendered_findings))
        system_rows.append(
            binding.fill(
                body,
                {
                    "system_name": escape(system.name),
                    "system_blurb": escape(system.blurb.strip()),
                    "system_summary": _system_summary(len(findings), system.name),
                },
                where="systems",
            )
        )

    legend_region = binding.find_region(template_html, "legend")
    legend_html = binding.render_region(
        legend_region,
        [
            {
                "severity_label": escape(s.label),
                "severity_description": escape(s.description.strip()),
            }
            for s in catalogue.severities()
        ],
        where="legend",
    )

    out = binding.replace_region(template_html, "system", "".join(system_rows))
    out = binding.replace_region(out, "legend", legend_html)

    licence = inspection.inspector.licence_number.strip()
    return binding.fill(
        out,
        {
            "firm_name": escape(inspection.inspector.firm_name or "Independent inspection"),
            "property_address": escape(inspection.property.one_line()),
            "inspector_name": escape(inspection.inspector.name),
            "licence_suffix": f" (licence {escape(licence)})" if licence else "",
            "inspected_on": inspection.inspected_on.strftime("%d %B %Y"),
            "preamble": PREAMBLE,
            "limits": LIMITS,
            "systems_inspected": ", ".join(s.name for s in catalogue.systems()) + ".",
        },
        where="report",
    )
