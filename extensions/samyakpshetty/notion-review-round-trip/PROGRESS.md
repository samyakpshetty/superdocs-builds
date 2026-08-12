# Progress & engineering notes

## What this is

A Notion ⇄ Word review-cycle round-trip, built on the SuperDocs API. A team keeps its source of
truth in Notion; its formal reviewers work in Microsoft Word. This integration sends a Notion page
out as a styled Word document, reads the reviewer's tracked changes and comments when it comes
back, and proposes each change onto the exact Notion block for item-by-item approval — preserving
Notion structure and reviewer attribution throughout.

## Status

The assigned build runs end to end on the deterministic providers and on the live services, covered
by tests that need no API key: outbound (Notion → Word), inbound markup parsing, per-change proposal
through SuperDocs, an item-by-item human gate, and verified write-back to Notion. It holds to a
production bar: a change lands on the exact block or is surfaced, never on the wrong one; a block
edited in Notion while it was out for review is a surfaced conflict, never a silent overwrite; a
re-run after a crash re-spends no operation and double-applies nothing; a paused review resumes at
the gate from a durable checkpoint; write-back preserves the block's other formatting; and a packet
can carry several pages, each change fanning back to its own page. 83 keyless tests plus a
Postgres-backed store test; `ruff`, `mypy --strict`, and `pytest` all green; one documented command
takes a fresh clone to a working demo.

## Architecture

- **Two provider seams behind typed protocols** — a Notion client and a SuperDocs client — each
  with a deterministic in-memory implementation (keyless tests and demo) and a live HTTP
  implementation. Selection is a single config flag.
- **Outbound**: fetch the page's block tree via Notion's API → render to HTML that preserves
  structure (toggles, callouts, tables, inline databases) and rich-text, recording a reversible
  block map → upload to SuperDocs → export a styled `.docx` for the reviewer.
- **Inbound**: parse the returned `.docx`'s tracked changes and comments from raw OOXML → match
  each change to its block by the as-sent text → propose the edit through SuperDocs → **human
  gate** → write approved changes back to Notion, range by range.
- **Durable execution**: the inbound review is a LangGraph graph with a checkpointer and a real
  interrupt at the approval gate, so a run survives a crash or a days-long pause and resumes from
  exactly where it stopped — from a file-backed SQLite checkpoint by default, Postgres for scale.
- **Persistence**: each review round is a row in a store behind a protocol — SQLite by default
  (zero-infra), Postgres for scale — chosen by `DATABASE_URL`. Each row carries a version, and a
  write is an optimistic compare-and-set, so concurrent work on one round is rejected rather than
  lost; distinct rounds are isolated by row.
- **Multi-document**: a review packet renders several Notion pages into one Word file; every block
  carries the page it came from, so an approved change fans back to the exact block on the exact
  page. Each page keeps its own review-round link and completion record.

## Decisions

- **LangGraph with a checkpointer for the inbound flow.** The review is a long-lived, human-gated
  process; durable execution with an interrupt gate is the right primitive for it. A dedicated
  workflow engine (Temporal and the like) is the documented scale path once volume warrants it.
- **SuperDocs is the only metered/AI call in the system.** Orchestration is deterministic code, so
  cost is centralised and auditable, and the whole test suite and demo run for free on the fakes.
- **Write-back is per-block, verified by read-back, and surgical.** Only blocks a reviewer changed
  are ever touched; a change is marked applied only after the block is re-read and confirmed, so a
  success message always reflects the real state. The reviewer's edit is spliced into the block so
  only the differing span changes and the surrounding formatting (bold, links, colour) survives.
- **Changes are matched positionally within a text.** The n-th block carrying a given as-sent text
  pairs with the n-th same-text paragraph, so an edit to the second of two identical paragraphs
  lands on the second block; genuine ambiguity is surfaced, never guessed.
- **A drifted block is a conflict, not a clobber.** Before write-back a block is confirmed to still
  hold the text that was sent; if the page changed in the meantime the change is surfaced for human
  resolution rather than overwriting the newer edit.
- **Idempotency wherever an operation costs money.** Each change carries a content hash, so a
  re-run never re-spends an operation or double-applies a change.
- **One batched call per round, best-effort.** A round's edits go to SuperDocs in a single request
  (one operation covers up to 25 sections), not one chat per change, and it is retried on the
  engine's transient "at capacity" failure. Because the reviewer's text is authoritative and is
  written to Notion directly, this metered call never gates the review: a SuperDocs quota limit or
  outage degrades gracefully rather than parking the round or discarding a change. A per-round
  ceiling and a sample mode bound the cost.
- **The returned `.docx` is treated as untrusted input.** It is read behind zip-bomb and XXE
  defenses before any parsing.

## Accepted inputs

- Documents: a Notion page (its block tree) goes out; a reviewer's Word `.docx` (tracked changes
  and comments) comes back. A second run means a different Notion page and different reviewer
  markup within that shape.
- Structures carried across the round-trip: headings, paragraphs, quotes, lists, to-dos, code,
  toggles, callouts, tables, and inline databases (preserved, never editable as text).

## Assumptions & limitations

- **Block ↔ chunk mapping** relies on a per-block marker that survives the round-trip; a positional
  fallback covers the case where the marker is stripped.
- **Change → block matching** is by the as-sent paragraph text, resolved positionally when a text
  repeats; a change that cannot be located is surfaced, never silently dropped.
- **Tables** round-trip structurally and their rows are mapped. SuperDocs re-chunks a table as a
  single unit on upload, so an edit targeted at one cell cannot be isolated back to that cell; this
  is a SuperDocs-side limit, surfaced rather than guessed.
- **Write-back** splices the reviewer's edit in place, preserving the block's surrounding formatting;
  a change that rewrites a block whole degrades to plain text and records that in the provenance.
- **Comment-only feedback** is applied as an attributed Notion comment; turning a free-form comment
  into a concrete proposed edit through SuperDocs is the next build (comments-as-intent).

## Running it

```bash
make check   # ruff + mypy (strict) + the full keyless test suite
make demo    # the whole round-trip on the fake providers — no keys, no operations
```

For a live run, copy `.env.example` to `.env`, add your SuperDocs and Notion keys, and use the live
demo target. `.env` is git-ignored; no secret is ever committed.

## Roadmap

- Guided three-way merge: drift is already detected and surfaced as a conflict; the next step is to
  reconcile the reviewer's edit against the newer Notion text rather than only flagging it.
- Comments-as-intent: a reviewer's free-form comment becomes a concrete proposed edit through
  SuperDocs — the case where the product itself writes the change.
- A web review console and an MCP surface, so a person in a browser or another agent can drive the
  same gate the CLI drives today.
