# Progress & engineering notes

My working log for this build: what it does, the calls I made, and where I decided to stop.

## What this is

A Notion ⇄ Word review-cycle round-trip, built on the SuperDocs API. A team keeps its source of
truth in Notion; its formal reviewers work in Microsoft Word. This sends a Notion page out as a
styled Word document, reads the reviewer's tracked changes and comments when it comes back,
proposes each change through SuperDocs, and lands the approved ones on the exact Notion block —
preserving structure and reviewer attribution throughout.

## Status

Complete, and proven end to end on both the deterministic providers and the live services.
**135 tests that need no API key**, plus a Postgres-backed store test; `ruff`, `mypy --strict` and
`pytest` all green. I verified the one-command claim by cloning the repository fresh, with no
`.env`: `make check` and `make demo` both run unchanged.

I have driven the full cycle live on a real Notion page several times. Tracked changes and
AI-authored comment edits landed on the right blocks; a rejected change left its block untouched;
a second round editing the same blocks was refused by the drift guard rather than overwriting the
first; and a returned document was picked up and applied with nobody at a terminal.

## Architecture

- **Two provider seams behind typed protocols** — a Notion client and a SuperDocs client — each
  with a deterministic in-memory implementation for the keyless suite and demo, and a live HTTP
  implementation. Selection is one config flag.
- **Outbound**: fetch each page's block tree → render to HTML preserving structure (headings,
  lists, to-dos, toggles, callouts, tables, code, inline databases) and rich text, recording a
  reversible block map → upload the whole document to SuperDocs → export a styled `.docx` → stamp
  the review-round id into it → record the round and link it on the page.
- **Inbound**: parse the returned `.docx`'s tracked changes and comments from raw OOXML → match
  each change to its block → propose it through SuperDocs in review mode → **human gate** → write
  approved changes back to Notion, block by block.
- **SuperDocs does the editing, not me.** A tracked change goes out as a scoped edit; a directive
  comment is handed to SuperDocs' AI, which *authors* the concrete edit and returns it with an
  explanation. All four contract calls are exercised: upload, chat, approve, export.
- **One headless gate, three drivers**: a review queue *in Notion*, a comment on the changed line
  itself, and a terminal gate for CI or an agent. Decisions from any of them are merged.
- **The whole cycle runs unattended.** A request row in Notion sends a page out; the document is
  delivered to a folder reviewers can reach; a returned file is matched by the id it carries,
  proposed, and queued; the owner's decisions are read back and applied.
- **Durable execution**: the inbound review is a LangGraph graph with a checkpointer and a real
  interrupt at the gate, so a run survives a crash or a days-long pause and resumes where it
  stopped.
- **Persistence**: each round is a row in a store behind a protocol — SQLite by default, Postgres
  for scale — with an optimistic version, so concurrent work is rejected rather than lost.

## Decisions

- **LangGraph with a checkpointer for the inbound flow.** The review is a long-lived, human-gated
  process, and durable execution with an interrupt gate is the right primitive for it. A dedicated
  workflow engine is the scale path once volume warrants it.
- **SuperDocs is the only metered call.** Orchestration is deterministic code, so cost is
  centralised and auditable, and the whole test suite and demo run for free on the fakes.
- **I built on the REST API rather than MCP.** The card names API as a surface and the brief treats
  the two as interchangeable; REST was the shorter path to the four calls. An MCP surface over the
  same controller is a natural addition, not a rewrite.
- **Write-back is per-block, verified by read-back, and surgical.** Only blocks a reviewer changed
  are touched; a change is marked applied only after the block is re-read and confirmed; the edit
  is spliced in so surrounding formatting survives.
- **A drifted block is a conflict, not a clobber.** If the page changed while it was out for
  review, the change is surfaced for a human instead of overwriting the newer edit.
- **Approval is an operation, not a screen.** The controller exposes it and the interface is a
  driver — which is why the same gate runs from Notion, from a comment thread, and from a terminal.
- **The returned file carries its own round id**, so any return channel works without a human
  quoting an identifier.
- **Idempotency wherever an operation costs money.** Each change carries a content hash, so a
  re-run after a crash re-spends nothing and double-applies nothing.
- **The `.docx` is untrusted input**: read behind zip-bomb and XXE defenses, reviewer text
  sanitised before it reaches the AI, links surfaced at the gate, and a size cap at the write.

## Assumptions I logged along the way

Where the brief or the API was silent, I made a call and recorded it.

1. **A SuperDocs session holds one pending proposal set at a time.** I verified this live: with
   proposals pending, a second chat on that session returns `409 session_busy` and never clears.
   So a review larger than one operation goes out batch by batch, each gated and applied before
   the next — which is also the card's "one approval at a time".
2. **`approve` keys each change on `change_id`, not `chunk_id`.** The documentation says
   otherwise, and approving by `chunk_id` returns a 500. I reverse-engineered the real contract
   and verified it against the live API.
3. **One operation per request that changed something**, when the API returns no usage block.
   Otherwise a per-round budget could never trip. This follows the documented billing rule.
4. **A reviewer's question is not an edit request.** A comment ending in "?" goes to the page owner
   rather than to the AI, which could otherwise fabricate an answer into the document. The
   heuristic is deliberately conservative and the human gate is the backstop.
5. **Decisions are polled, not pushed.** Notion's button blocks are unsupported by its public API,
   so a database row is what an integration can actually offer. Polling suits a review measured in
   hours or days and keeps this to one moving part, with no public endpoint to expose.
6. **Reviewer attribution is provenance, not authenticated identity.** Word author names are
   self-declared strings; I report them faithfully and claim nothing more.
7. **Delivery is a seam with a folder implementation.** Synced to a shared drive that is a real
   channel. I deliberately did not ship an email sender without an email intake: telling a reviewer
   to reply to a mailbox nothing reads is a promise the system cannot keep.

## Accepted inputs

- **Documents**: a Notion page — or several as one review packet — goes out; a reviewer's Word
  `.docx` with tracked changes and comments comes back. A second run means different Notion pages
  and different reviewer markup within that shape.
- **Structures carried across the round-trip**: headings, paragraphs, quotes, bulleted and numbered
  lists, to-dos, code, toggles, callouts, tables, and inline databases.

## Limitations

- **One workspace per deployment.** A single Notion integration token rather than per-workspace
  OAuth; multi-tenancy is not built.
- **Email is not a channel yet.** Documents move through folders, which is real when synced but
  not the same as a reviewer replying to a message. Both halves belong behind the existing seams.
- **Table cells cannot be targeted individually.** SuperDocs re-chunks a whole table as one unit on
  upload, so an edit aimed at a single cell cannot be isolated back to that cell. It is surfaced
  rather than guessed at.
- **A toggle's summary text is dropped by SuperDocs on upload**, so an edit to a toggle title
  cannot round-trip. Both of these are SuperDocs-side and reported as bugs.
- **Decisions apply within one poll interval** (ten seconds by default), not instantly.
- **No notifications**: the page is commented and updated, but nobody is emailed.

## Running it

```bash
make check           # ruff + mypy (strict) + the full keyless test suite
make demo            # the whole round-trip on the fake providers — no keys, no operations
make test-postgres   # the store against a real Postgres
```

For a live run, copy `.env.example` to `.env` and add the SuperDocs and Notion keys. `.env` is
git-ignored, no secret is committed, and every log line is scrubbed of tokens before it is emitted.
`notion-review status` is where I look first when something needs investigating: it shows every
round, what became of each change, why anything failed, and the ids to trace it through both
services.

## What I would build next

- Email as a channel, both directions at once — delivery and intake behind the seams that exist.
- Per-workspace OAuth and multi-tenancy.
- A guided three-way merge: drift is detected and surfaced today; reconciling a reviewer's edit
  against newer Notion text is the next step.
- An MCP surface, so another agent can drive the same gate the CLI drives.
