# Progress & engineering notes

## What this is

A Notion ⇄ Word review-cycle round-trip, built on the SuperDocs API. A team keeps its source of
truth in Notion; its formal reviewers work in Microsoft Word. This integration sends a Notion page
out as a styled Word document, reads the reviewer's tracked changes and comments when it comes
back, and proposes each change onto the exact Notion block for item-by-item approval — preserving
Notion structure and reviewer attribution throughout.

## Status

The assigned build is complete end to end on the deterministic providers and covered by tests that
run with no API key: outbound (Notion → Word), inbound markup parsing, per-change proposal through
SuperDocs, an item-by-item human gate, and verified write-back to Notion. 60 tests (59 keyless plus
a Postgres-backed store test); `ruff`, `mypy --strict`, and `pytest` all green; one documented
command takes a fresh clone to a working demo.

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
  exactly where it stopped.
- **Persistence**: each review round is a row in a store behind a protocol — SQLite by default
  (zero-infra), Postgres for scale — chosen by `DATABASE_URL`.

## Decisions

- **LangGraph with a checkpointer for the inbound flow.** The review is a long-lived, human-gated
  process; durable execution with an interrupt gate is the right primitive for it. A dedicated
  workflow engine (Temporal and the like) is the documented scale path once volume warrants it.
- **SuperDocs is the only metered/AI call in the system.** Orchestration is deterministic code, so
  cost is centralised and auditable, and the whole test suite and demo run for free on the fakes.
- **Write-back is per-block, verified by read-back.** Only blocks a reviewer changed are ever
  touched; a change is marked applied only after the block is re-read and confirmed, so a success
  message always reflects the real state.
- **Idempotency wherever an operation costs money.** Each change carries a content hash, so a
  re-run never re-spends an operation or double-applies a change.
- **Budget guards.** Changes are batched to the fewest operations, with a per-round ceiling and a
  circuit-breaker that parks a round on quota exhaustion instead of half-applying it.
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
- **Change → block matching** is by the as-sent paragraph text; a change that cannot be located is
  surfaced, never silently dropped.
- **Tables** round-trip structurally and their rows are mapped; cell-level edit mapping is a known
  limitation, not yet wired.
- **Write-back** applies the reviewer's text; carrying inline styling through the write-back is a
  documented extension.
- **Comment-only feedback** is applied as an attributed Notion comment; turning a free-form comment
  into a concrete proposed edit is a planned extension.

## Running it

```bash
make check   # ruff + mypy (strict) + the full keyless test suite
make demo    # the whole round-trip on the fake providers — no keys, no operations
```

For a live run, copy `.env.example` to `.env`, add your SuperDocs and Notion keys, and use the live
demo target. `.env` is git-ignored; no secret is ever committed.

## Roadmap

- Live end-to-end run against real Notion and SuperDocs, captured with screenshots.
- Drift-aware three-way merge for when the Notion page changes while it is out for review.
- Comments-as-intent: a reviewer's comment becomes a concrete proposed edit through SuperDocs.
- A web review console and an MCP surface, so an agent can drive the whole cycle.
