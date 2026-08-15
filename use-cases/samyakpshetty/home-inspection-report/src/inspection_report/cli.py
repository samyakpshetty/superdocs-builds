"""Command line: build the sample report, and verify what came out.

``demo`` runs the whole thing on the deterministic fake — no key, no cost — and writes real
files into a mounted directory, because anything a ``--rm`` container writes outside a
mounted path is destroyed when it exits.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from inspection_report import sample
from inspection_report.logging import get_logger, setup_logging
from inspection_report.render import pipeline
from inspection_report.superdocs.base import SuperDocsClient
from inspection_report.superdocs.fake import FakeSuperDocsClient
from inspection_report.superdocs.live import LiveSuperDocsClient
from inspection_report.verify import exports as verify_exports

_log = get_logger("inspection_report.cli")

EXPORT_DIR = Path(os.environ.get("EXPORT_DIR", "exports"))
TEMPLATE_DIR = Path(os.environ.get("TEMPLATE_DIR", "templates"))


def build_client() -> SuperDocsClient:
    """The provider seam. Defaults to the fake, which is why no key is ever required."""
    if os.environ.get("PROVIDER", "fake").lower() == "live":
        return LiveSuperDocsClient(api_key=os.environ.get("SUPERDOCS_API_KEY", ""))
    return FakeSuperDocsClient()


@click.group()
def main() -> None:
    """A structured home-inspection report builder, built on SuperDocs."""
    setup_logging(os.environ.get("LOG_FORMAT", "console"))


@main.command()
@click.option("--template", default="buyer_summary", help="Report format to use.")
@click.option("--polish/--no-polish", default=True, help="Run the AI rewrite pass.")
@click.option(
    "--model-tier",
    default="core",
    type=click.Choice(["core", "turbo", "pro", "max"]),
    help="Precision/speed. Never hard-coded — see the README.",
)
def demo(template: str, polish: bool, model_tier: str) -> None:
    """Build the sample report end to end and verify the exported files."""
    inspection = sample.sample_inspection()
    photo_data = sample.sample_photo_data()
    template_path = TEMPLATE_DIR / f"{template}.html"
    if not template_path.exists():
        available = sorted(p.stem for p in TEMPLATE_DIR.glob("*.html"))
        raise click.ClickException(
            f"no report format called {template!r}. Available: {available}. "
            f"Formats live in {TEMPLATE_DIR}/."
        )

    client = build_client()
    provider = os.environ.get("PROVIDER", "fake")
    click.echo(
        f"Building {inspection.property.one_line()} — "
        f"{len(inspection.findings)} findings, {inspection.photo_count()} photographs, "
        f"provider={provider}, format={template}"
    )

    result = pipeline.build(
        inspection,
        template_path.read_text(),
        photo_data,
        client,
        session_id=f"inspection-{inspection.id.hex[:12]}",
        polish=polish,
        model_tier=model_tier,
    )

    click.echo(f"  photographs: {result.photos_uploaded} uploaded, {result.photos_reused} reused")
    if polish:
        approved = sum(1 for p in result.proposals if p.approved)
        click.echo(
            f"  rewrites: {len(result.proposals)} proposed, {approved} approved, "
            f"{result.rail_refusals} refused by the language rail"
        )
        for p in result.proposals:
            if p.refused_by_rail:
                click.echo(f"    refused — {p.verdict.summary()}")
        if result.ops_remaining is not None:
            click.echo(
                f"  operations: {result.ops_charged} charged, {result.ops_remaining} remaining"
            )

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ok = True
    for fmt, export in result.exports.items():
        path = EXPORT_DIR / export.filename
        path.write_bytes(export.content)
        click.echo(f"  wrote {path} ({len(export.content):,} bytes)")
        report = verify_exports.verify(data=export.content, fmt=fmt, inspection=inspection)
        click.echo(report.render())
        ok = ok and report.passed

    client.close()
    click.echo("\nAll checks passed." if ok else "\nSome checks FAILED.")
    if not ok:
        sys.exit(1)


@main.command()
@click.option("--template", default="buyer_summary")
def verify(template: str) -> None:
    """Re-verify the files already in the export directory."""
    inspection = sample.sample_inspection()
    for finding in inspection.findings:
        for photo in finding.photos:
            photo.remote_url = f"local://{photo.sha256}"  # presence, not fetchability

    found = sorted(EXPORT_DIR.glob("*.pdf")) + sorted(EXPORT_DIR.glob("*.docx"))
    if not found:
        raise click.ClickException(f"nothing to verify in {EXPORT_DIR}/. Run `make demo` first.")
    ok = True
    for path in found:
        report = verify_exports.verify(
            data=path.read_bytes(), fmt=path.suffix.lstrip("."), inspection=inspection
        )
        click.echo(f"{path.name}")
        click.echo(report.render())
        ok = ok and report.passed
    if not ok:
        sys.exit(1)


@main.command()
def formats() -> None:
    """List the report formats this build ships."""
    for path in sorted(TEMPLATE_DIR.glob("*.html")):
        click.echo(f"  {path.stem}")


if __name__ == "__main__":
    main()
