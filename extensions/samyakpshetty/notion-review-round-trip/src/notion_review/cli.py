"""Command-line surface for the review round-trip.

``demo`` runs the whole thing keyless on the fake providers — send a Notion page for review,
read a marked-up Word file, and approve each change item by item — so a reviewer of this project
can watch the round-trip end to end without any setup. It is also what ``make demo`` runs.
"""

from __future__ import annotations

import click

from notion_review.config import Config
from notion_review.domain import ChangeSource, ProposalStatus, ProposedChange
from notion_review.logging import setup_logging
from notion_review.notion.base import NotionClient
from notion_review.roundtrip import InboundController, send_for_review
from notion_review.roundtrip.inbound import plain_text_from_html
from notion_review.sample import demo_page, demo_review_docx
from notion_review.store import SQLiteStore
from notion_review.superdocs import FakeSuperDocsClient


@click.group()
def main() -> None:
    """Notion ⇄ Word review-cycle round-trip, built on SuperDocs."""


@main.command()
@click.option("--interactive", is_flag=True, help="Approve each change by hand instead of all.")
def demo(interactive: bool) -> None:
    """Run the full round-trip on the fake providers — no keys, no operations."""
    setup_logging("console")
    notion, page_id = demo_page()
    superdocs = FakeSuperDocsClient()
    store = SQLiteStore()

    click.secho("\n1. Sending the Notion page out for review…", fg="cyan", bold=True)
    packet = send_for_review(notion=notion, superdocs=superdocs, store=store, page_id=page_id)
    click.echo(
        f"   → review round {packet.round.id}: "
        f"{len(packet.round.block_map)} blocks, {len(packet.docx.content):,}-byte Word file."
    )

    click.secho("\n2. Reading the reviewer's marked-up Word file…", fg="cyan", bold=True)
    controller = InboundController(
        notion=notion, superdocs=superdocs, store=store, config=Config.from_env({})
    )
    gate = controller.start(round_id=packet.round.id, docx_bytes=demo_review_docx())
    click.echo(f"   → {len(gate.pending)} change(s) proposed, awaiting your approval.\n")

    decisions = _collect_decisions(gate.pending, interactive=interactive)

    click.secho("3. Applying the approved changes to Notion…", fg="cyan", bold=True)
    final = controller.submit(round_id=packet.round.id, decisions=decisions)
    _print_outcome(final.proposals, notion)
    click.secho(f"\nRound {final.id} finished: {final.status.value}.", fg="green", bold=True)


def _collect_decisions(
    pending: list[ProposedChange], *, interactive: bool
) -> list[dict[str, object]]:
    decisions: list[dict[str, object]] = []
    for i, proposal in enumerate(pending, start=1):
        _print_card(i, proposal)
        approved = click.confirm("   Approve?", default=True) if interactive else True
        if not interactive:
            click.echo("   → approved (pass --interactive to decide each one)")
        decisions.append({"proposal_id": proposal.id, "approved": approved})
    return decisions


def _print_card(number: int, proposal: ProposedChange) -> None:
    click.secho(f"  [{number}] {proposal.reviewer_name}", fg="yellow", bold=True)
    if proposal.source == ChangeSource.TRACKED_CHANGE:
        click.echo(f"      was: {_text(proposal.old_html)}")
        click.echo(f"      now: {_text(proposal.new_html)}")
    else:
        click.echo(f"      comment: {proposal.reviewer_comment}")


def _print_outcome(proposals: list[ProposedChange], notion: NotionClient) -> None:
    for proposal in proposals:
        icon = {
            ProposalStatus.APPLIED: "✓ applied",
            ProposalStatus.REJECTED: "✗ rejected",
            ProposalStatus.FAILED: "! failed",
        }.get(proposal.status, proposal.status.value)
        block = notion.retrieve_block(proposal.notion_block_id)
        click.echo(f"   {icon}: {proposal.reviewer_name} → “{block.plain()[:60]}”")


def _text(html: str) -> str:
    return plain_text_from_html(html) or "(removed)"


if __name__ == "__main__":
    main()
