"""Command-line surface for the review round-trip.

``demo`` runs the whole thing keyless on the fake providers — send a Notion page for review,
read a marked-up Word file, and approve each change item by item — so a reviewer of this project
can watch the round-trip end to end without any setup. It is also what ``make demo`` runs.
"""

from __future__ import annotations

from pathlib import Path

import click

from notion_review.clients import build_clients
from notion_review.config import Config
from notion_review.domain import ChangeSource, ProposalStatus, ProposedChange, ReviewRound
from notion_review.logging import setup_logging
from notion_review.notion.base import NotionClient
from notion_review.roundtrip import (
    InboundController,
    ReviewGate,
    send_for_review,
    send_packet_for_review,
)
from notion_review.roundtrip.checkpoint import open_checkpointer
from notion_review.roundtrip.inbound import plain_text_from_html
from notion_review.roundtrip.notion_gate import (
    await_decisions,
    publish_pending,
    record_outcomes,
)
from notion_review.sample import demo_page, demo_review_docx
from notion_review.store import SQLiteStore
from notion_review.superdocs import FakeSuperDocsClient

_STATE_DEFAULT = ".notion-review-state.db"


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

    click.secho("3. Approving each change, then applying it to Notion…", fg="cyan", bold=True)
    final = _run_gates(controller, round_id=packet.round.id, gate=gate, interactive=interactive)
    _print_outcome(final.proposals, notion)
    click.echo(f"\n   cost: {final.cost_summary()}")
    click.secho(f"Round {final.id} finished: {final.status.value}.", fg="green", bold=True)


@main.command()
@click.option(
    "--page-id",
    "page_ids",
    required=True,
    multiple=True,
    help="A Notion page id to send for review. Repeat to send several pages as one packet.",
)
@click.option(
    "--out", default="review.docx", type=click.Path(), help="Where to write the Word file."
)
@click.option("--state", default=_STATE_DEFAULT, type=click.Path(), help="Round store file.")
def send(page_ids: tuple[str, ...], out: str, state: str) -> None:
    """Send one or more Notion pages out for review; writes a Word file and records the round.

    Uses the live providers when PROVIDER=live, otherwise the fakes.
    """
    config = Config.from_env()
    setup_logging(config.log_format)
    notion, superdocs = build_clients(config)
    store = SQLiteStore(state)
    packet = send_packet_for_review(
        notion=notion, superdocs=superdocs, store=store, page_ids=list(page_ids)
    )
    Path(out).write_bytes(packet.docx.content)
    click.secho(f"Sent for review. round={packet.round.id}", fg="green", bold=True)
    click.echo(
        f"  {len(page_ids)} page(s) · {len(packet.round.block_map)} blocks · Word file → {out}"
    )
    click.echo("  Mark it up in Word (tracked changes + comments), then run:")
    click.echo(f"    notion-review review --round-id {packet.round.id} --markup {out}")


@main.command()
@click.option("--round-id", required=True, help="The review round id printed by `send`.")
@click.option("--markup", required=True, type=click.Path(exists=True), help="The marked-up .docx.")
@click.option("--state", default=_STATE_DEFAULT, type=click.Path(), help="Round store file.")
@click.option("--interactive", is_flag=True, help="Approve each change by hand.")
def review(round_id: str, markup: str, state: str, interactive: bool) -> None:
    """Apply a reviewer's marked-up Word file back onto the Notion page, with approval."""
    config = Config.from_env()
    setup_logging(config.log_format)
    notion, superdocs = build_clients(config)
    store = SQLiteStore(state)
    # A durable graph checkpoint beside the state file: if this command is killed at the gate,
    # a re-run resumes there instead of re-proposing (and so never re-spends an operation).
    controller = InboundController(
        notion=notion,
        superdocs=superdocs,
        store=store,
        config=config,
        checkpointer=open_checkpointer(f"{state}.ckpt"),
    )
    gate = controller.start(round_id=round_id, docx_bytes=Path(markup).read_bytes())
    click.echo(f"{len(gate.pending)} change(s) proposed.\n")
    final = _run_gates(controller, round_id=round_id, gate=gate, interactive=interactive)
    _print_outcome(final.proposals, notion)
    click.echo(f"\n   cost: {final.cost_summary()}")
    click.secho(f"Round {final.id} finished: {final.status.value}.", fg="green", bold=True)


@main.command("review-in-notion")
@click.option("--round-id", required=True, help="The review round id printed by `send`.")
@click.option("--markup", required=True, type=click.Path(exists=True), help="The marked-up .docx.")
@click.option("--state", default=_STATE_DEFAULT, type=click.Path(), help="Round store file.")
@click.option("--poll", default=15.0, help="Seconds between checks for the owner's decisions.")
@click.option("--timeout", default=86_400.0, help="Give up waiting after this many seconds.")
def review_in_notion(round_id: str, markup: str, state: str, poll: float, timeout: float) -> None:
    """Approve changes inside Notion: each one becomes a row you set to Approved or Rejected.

    The page owner never leaves Notion — no second app, no extra login. This command publishes the
    queue, waits for the decisions, applies the approved changes, and records each outcome.
    """
    config = Config.from_env()
    setup_logging(config.log_format)
    notion, superdocs = build_clients(config)
    store = SQLiteStore(state)
    controller = InboundController(
        notion=notion,
        superdocs=superdocs,
        store=store,
        config=config,
        checkpointer=open_checkpointer(f"{state}.ckpt"),
    )
    gate = controller.start(round_id=round_id, docx_bytes=Path(markup).read_bytes())
    final = gate.round
    batch = 0
    while gate.pending:
        batch += 1
        added = publish_pending(gate.round, notion, store)
        click.secho(f"\nBatch {batch}: {added} change(s) waiting in Notion.", fg="cyan", bold=True)
        click.echo("  Open the “Review queue” database on the page and set each Status.")

        decisions = await_decisions(
            gate.round,
            notion,
            poll_interval_s=poll,
            timeout_s=timeout,
            on_wait=lambda done, total: click.echo(f"   … {done}/{total} decided"),
        )
        if not decisions:
            click.secho(
                "  No decisions yet — stopping; re-run to pick up where you left off.", fg="yellow"
            )
            return
        final = controller.submit(round_id=round_id, decisions=decisions)
        record_outcomes(final, notion, final.proposals)
        store.save(final)
        gate = ReviewGate(round=final, pending=final.pending())

    _print_outcome(final.proposals, notion)
    click.echo(f"\n   cost: {final.cost_summary()}")
    click.secho(f"Round {final.id} finished: {final.status.value}.", fg="green", bold=True)


@main.command()
def whoami() -> None:
    """Check which SuperDocs account the configured API key belongs to."""
    from notion_review.superdocs.base import SuperDocsError
    from notion_review.superdocs.live import LiveSuperDocsClient

    config = Config.from_env()
    if not config.superdocs_api_key:
        raise click.ClickException("SUPERDOCS_API_KEY is not set (see .env.example)")
    client = LiveSuperDocsClient(
        api_key=config.superdocs_api_key, base_url=config.superdocs_base_url, config=config
    )
    try:
        click.echo(client.whoami())
    except SuperDocsError as exc:
        click.secho(f"whoami unavailable for this key: {exc}", fg="yellow")
    finally:
        client.close()


def _run_gates(
    controller: InboundController,
    *,
    round_id: str,
    gate: ReviewGate,
    interactive: bool,
) -> ReviewRound:
    """Drive every approval gate to the end of the round.

    A review larger than one SuperDocs operation is proposed batch by batch — each batch is gated
    and applied before the next goes out — so the driver keeps approving until nothing is pending.
    """
    batch = 0
    final = gate.round
    while gate.pending:
        batch += 1
        if batch > 1:
            click.secho(f"\nBatch {batch}: {len(gate.pending)} more change(s).", fg="cyan")
        decisions = _collect_decisions(gate.pending, interactive=interactive)
        final = controller.submit(round_id=round_id, decisions=decisions)
        gate = ReviewGate(round=final, pending=final.pending())
    return final


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
    if proposal.source == ChangeSource.COMMENT:
        click.echo(f"      comment: {proposal.reviewer_comment}")
    else:
        if proposal.source == ChangeSource.COMMENT_INTENT:
            click.echo(f"      request: {proposal.reviewer_comment}")
            if proposal.ai_explanation:
                click.echo(f"      SuperDocs: {proposal.ai_explanation}")
        click.echo(f"      was: {_text(proposal.old_html)}")
        click.echo(f"      now: {_text(proposal.new_html)}")
    for link in proposal.links:  # surface any URL an edit would introduce, before it lands
        click.secho(f"      ⚠ link: {link}", fg="red")


def _print_outcome(proposals: list[ProposedChange], notion: NotionClient) -> None:
    for proposal in proposals:
        icon = {
            ProposalStatus.APPLIED: "✓ applied",
            ProposalStatus.REJECTED: "✗ rejected",
            ProposalStatus.CONFLICT: "⚠ conflict (page changed)",
            ProposalStatus.FAILED: "! failed",
        }.get(proposal.status, proposal.status.value)
        block = notion.retrieve_block(proposal.notion_block_id)
        click.echo(f"   {icon}: {proposal.reviewer_name} → “{block.plain()[:60]}”")


def _text(html: str) -> str:
    return plain_text_from_html(html) or "(removed)"


if __name__ == "__main__":
    main()
