# Progress & engineering notes

## What this is

A Notion ⇄ Word review-cycle round-trip, built on the SuperDocs API. A team keeps its source of
truth in Notion; its formal reviewers work in Microsoft Word. This integration sends a Notion page
out as a styled Word document, reads the reviewer's tracked changes and comments when it comes
back, proposes each change through SuperDocs, and lands the approved ones on the exact Notion
block — preserving structure and reviewer attribution throughout.

## Status

Complete and proven end to end, both on the deterministic providers and against the live services.
**128 keyless tests plus a Postgres-backed store test**; `ruff`, `mypy --strict` and `pytest` green;
a fresh `git clone` with no keys runs `make check` and `make demo` unchanged.

The full cycle has been driven live on a real Notion page several times: tracked changes and
AI-authored comment edits landing on the right blocks, a rejection leaving its block untouched, a
concurrent round refused by the drift guard, and a returned file picked up and applied with nobody
at a terminal.

## Architecture

- **Two provider seams behind typed protocols** — a Notion client and a SuperDocs client — each
  with a deterministic in-memory implementation (keyless tests and demo) and a live HTTP
  implementation. Selection is a single config flag.
- **Outbound**: fetch each page's block tree → render to HTML preserving structure (headings,
  lists, to-dos, toggles, callouts, tables, code, inline databases) and rich text, recording a
  reversible block map → upload the whole document to SuperDocs → export a styled `.docx` →
  stamp the review-round id into it → record the round and link it on the page.
- **Inbound**: parse the returned `.docx`'s tracked changes and comments from raw OOXML → match
  each change to its block → propose it through SuperDocs in review mode → **human gate** → write
  approved changes back to Notion, block by block.
- **SuperDocs is the engine, not a pipe.** A tracked change is proposed as a scoped edit; a
  directive comment is handed to SuperDocs' AI, which *authors* the concrete edit
  (comments-as-intent) and returns it with an explanation. All four contract calls are exercised:
  upload, chat, approve, export.
- **The gate has three drivers over one headless controller**: a review queue *in Notion* (the
  owner never leaves Notion), a comment on the changed line itself, and a terminal gate for CI or
  an agent. Decisions from any of them are merged.
- **Unattended service**: a returned file arriving is the trigger. One tick takes in whatever came
  back, proposes it, queues it in Notion, then reads the owner's decisions and applies them.
- **Durable execution**: the inbound review is a LangGraph graph with a checkpointer and a real
  interrupt at the gate, so a run survives a crash or a days-long pause and resumes exactly where
  it stopped.
- **Persistence**: each round is a row in a store behind a protocol — SQLite by default, Postgres
  for scale — with an optimistic version so concurrent work is rejected rather than lost.

## Decisions

- **LangGraph with a checkpointer for the inbound flow.** The review is a long-lived, human-gated
  process; durable execution with an interrupt gate is the right primitive. A dedicated workflow
  engine (Temporal and the like) is the documented scale path once volume warrants it.
- **SuperDocs is the only metered call.** Orchestration is deterministic code, so cost is
  centralised and auditable, and the whole test suite and demo run for free on the fakes.
- **Write-back is per-block, verified by read-back, and surgical.** Only blocks a reviewer changed
  are touched; a change is marked applied only after the block is re-read and confirmed; the edit
  is spliced in so surrounding formatting (bold, links, colour) survives.
- **A drifted block is a conflict, not a clobber.** If the page changed while it was out for
  review, the change is surfaced for a human instead of overwriting the newer edit.
- **Approval is an operation, not a screen.** The controller exposes it; the UI is a driver. That
  is what let the same gate be driven from Notion, from a comment thread, and from a terminal.
- **The returned file carries its own round id**, so any delivery channel works without a human
  quoting an identifier.
- **Idempotency wherever an operation costs money.** Each change carries a content hash; a re-run
  after a crash re-spends zero operations and double-applies nothing.
- **The `.docx` is untrusted input**, read behind zip-bomb and XXE defenses, with reviewer text
  sanitised before it reaches the AI, links surfaced at the gate, and a size cap at the write.

## Assumptions logged along the way

Where the brief or the API was silent, a call was made and recorded here.

1. **A SuperDocs session holds one pending proposal set at a time.** Verified live: with proposals
   pending, a second chat on that session returns `409 session_busy` that never clears. So a review
   larger than one operation is proposed batch by batch, each gated and applied before the next
   goes out — which is also the card's "one approval at a time".
2. **`approve` keys each change on `change_id`, not `chunk_id`.** The docs say otherwise; approving
   by `chunk_id` returns a 500. Reverse-engineered and verified against the live API.
3. **One operation per request that changed something**, when the API returns no usage block —
   otherwise the per-round budget could never trip. Follows the documented billing rule.
4. **A reviewer's question is not an edit request.** A comment ending in "?" is surfaced to the page
   owner rather than handed to the AI, which could otherwise fabricate an answer into the document.
   A conservative heuristic, with the human gate as the backstop.
5. **Decisions are collected by polling, not webhooks.** Fine for a review measured in hours or
   days, and it keeps the integration to one moving part with no public endpoint to expose.
6. **Reviewer attribution is provenance, not authenticated identity.** Word author names are
   self-declared strings; the integration reports them faithfully and claims nothing more.
7. **Delivery of the Word file to reviewers is out of scope.** The round-trip starts when the file
   is generated and resumes when it comes back; posting it to email or Slack is a wrapper.

## Accepted inputs

- **Documents**: a Notion page (or several as one review packet) goes out; a reviewer's Word
  `.docx` with tracked changes and comments comes back. A second run means different Notion pages
  and different reviewer markup within that shape.
- **Structures carried across the round-trip**: headings, paragraphs, quotes, bulleted and numbered
  lists, to-dos, code, toggles, callouts, tables, and inline databases.

## Limitations, honestly

- **Outbound delivery is manual.** The system generates and records; a person still attaches the
  file to an email. The return path is fully automated because the file identifies itself.
- **A single Notion integration token**, not per-workspace OAuth; multi-tenancy is not built.
- **Table cells cannot be targeted individually.** SuperDocs re-chunks a whole table as one unit on
  upload, so an edit aimed at one cell cannot be isolated back to that cell. Surfaced, not guessed.
- **A toggle's summary text is dropped by SuperDocs on upload**, so an edit to a toggle title
  cannot round-trip. Both of these are SuperDocs-side and reported as bugs.
- **Decision latency is one poll interval** (10s by default), not instant.
- **No notifications**: the page is updated and commented, but nobody is emailed.

## Running it

```bash
make check   # ruff + mypy (strict) + the full keyless test suite
make demo    # the whole round-trip on the fake providers — no keys, no operations
make test-postgres   # the store against a real Postgres
```

For a live run, copy `.env.example` to `.env` and add your SuperDocs and Notion keys. `.env` is
git-ignored; no secret is ever committed, and every log line is scrubbed of tokens before it is
emitted. `notion-review status` is the first place to look when something needs investigating.

## Roadmap

- Deliver the Word file and receive it back over email or Slack, closing the last manual link.
- Per-workspace OAuth and multi-tenancy.
- Guided three-way merge: drift is detected and surfaced today; reconciling the reviewer's edit
  against the newer Notion text is the next step.
- An MCP surface, so another agent can drive the same gate the CLI drives.
