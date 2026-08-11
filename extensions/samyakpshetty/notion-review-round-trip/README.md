# Notion ⇄ Word review-cycle round-trip

**Built on [SuperDocs](https://use.superdocs.app) for the SuperDocs task.**

Your team's source of truth lives in Notion. Your reviewers — legal, a client, an editor —
live in Microsoft Word and have never opened Notion in their life. This integration closes
that loop:

1. **Send a Notion page out for review.** The page (headings, toggles, callouts, inline
   databases) becomes a cleanly styled Word document.
2. **The reviewer works entirely in Word.** They mark it up the way they always have —
   tracked changes and comments — and send the `.docx` back. They never touch Notion.
3. **Each change comes back onto the exact Notion block, one approval at a time.** The
   integration reads the returned markup, has SuperDocs propose each edit against the
   original passage, and you approve or reject them item by item. Only approved changes are
   written back — range by range, never a wholesale page overwrite — with the reviewer's
   name preserved into a Notion comment. The page keeps a link to the review round.

SuperDocs is the editing engine in the middle; this project is the host wiring on both ends.

## What SuperDocs features it uses

- **Upload** (`/v1/documents/upload-base64`) — the Notion page enters SuperDocs as HTML;
  every block gets a stable chunk id for targeted editing.
- **Chat / edit** (`/v1/chat`, `/v1/chat/async`) — each reviewer change becomes a scoped,
  proposed edit on its block.
- **Approve** (`/v1/chat/{session}/approve`) — the item-by-item human gate.
- **Export** (`/v1/documents/export`) — the styled Word review copy the reviewer receives.

## Run it

Everything runs in Docker (Python 3.12). No API keys are needed for the tests or the demo —
they use deterministic fake providers, so nothing hits the network and no operations are spent.

```bash
make check   # ruff + mypy + full keyless test suite (the CI gate)
make demo    # a full fake round-trip over a real Postgres substrate — no keys, no ops
```

To run the real end-to-end round-trip against live Notion and SuperDocs, copy `.env.example`
to `.env`, fill in your keys, and:

```bash
make demo-live
```

`.env` is gitignored; no secret is ever committed.

## Status

Under active construction — see the build sequence in the project notes. This README grows a
screenshot and a demo-video link as the floor lands.
