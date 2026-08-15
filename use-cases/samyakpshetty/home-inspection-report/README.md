# Home-inspection report builder

**I built this for the SuperDocs task**, on the [SuperDocs](https://use.superdocs.app) API.

A home inspector walks a property system by system and types what they see, in shorthand,
often one-handed on a phone in an attic. The person who has to read it is buying their first
house and has never seen an inspection report before. This closes that gap: findings grouped
by inspection system, each carrying a severity label, the inspector's notes and the
photographs that show it, assembled into a formatted field report on export — and the
language stays observational, because an inspection reports what was seen and recommends what
to do next. It never certifies the condition of a property.

## What it does

1. **An inspection is structured data.** A property, an inspector, and findings, each one
   belonging to exactly one inspection system and carrying a severity, notes and photographs.
2. **Photographs are cleaned before they go anywhere.** Size is checked before the bytes are
   read, the image is decoded to prove it is an image, and **EXIF is stripped** — a phone
   photograph of a house carries the house's GPS coordinates, and this report goes to buyers,
   agents and lenders.
3. **The report is rendered deterministically into a format.** Which system a finding appears
   under, and in what order, is decided by code from the system catalogue and the severity
   ranks. No model touches structure.
4. **SuperDocs rewrites the field notes** into something a first-time buyer can read, as
   proposed changes held for review rather than applied.
5. **Every proposal passes an observational-language rail before it can be approved.** A
   rewrite that certifies, guarantees, declares something safe or compliant, predicts a
   lifespan, estimates a cost or reassures the reader is refused, and the inspector's own
   words stand. The report loses polish, never content.
6. **The export is read back and checked.** Every system present and in catalogue order,
   every finding under its own heading, every severity label intact, every photograph
   embedded as real image bytes, no capability URL in the text, and the rail still clean in
   the finished file.

## Running it

Everything runs in Docker, so the checks below behave the same on any machine.

```bash
make demo
```

That is the one command. It builds the sample property end to end on deterministic fakes —
**no API key, no network, no cost** — writes a real PDF and DOCX into `exports/`, then opens
those files and verifies them:

```
Building 14 Alder Lane, Fairhaven, FH8 2QR — 8 findings, 8 photographs, provider=fake, format=buyer_summary
  photographs: 8 uploaded, 0 reused
  rewrites: 8 proposed, 6 approved, 2 refused by the language rail
    refused — 'for safety' (safety); 'functioning correctly' (scoped)
    refused — "typical for the home's age" (reassurance)
  operations: 1 charged, 9999 remaining
  wrote exports/14-alder-lane-2026-08-12.pdf (88,023 bytes)
  PDF
    [PASS] every inspection system appears — 6 systems
    [PASS] systems appear in catalogue order
    [PASS] every finding is under its own system — 8 findings
    [PASS] severity labels are present — 4 distinct labels
    [PASS] photographs are embedded in the file — 8 embedded, 8 expected
    [PASS] no certification language in the exported file
    [PASS] no photo URLs leaked into the document text
```

```bash
make check
```

ruff + `ruff format --check` + `mypy --strict` + the full test suite: **106 tests, none of
which need an API key.**

Other targets: `make verify` re-checks the files already in `exports/`, and
`docker compose run --rm --no-deps api python -m inspection_report.cli formats` lists the
report formats.

<details>
<summary><b>Running it live against the real API</b></summary>

Get a key from `use.superdocs.app` → Settings → API Keys, then:

```bash
cp .env.example .env     # set PROVIDER=live and SUPERDOCS_API_KEY=your-key-here
docker compose run --rm --no-deps -e PROVIDER=live api python -m inspection_report.cli demo
```

The key is read server-side only and never reaches a browser. Choose the precision/speed
tier with `--model-tier core|turbo|pro|max`; it is a parameter rather than a constant,
because the right default for a legal document is not the right default for a quick pass.

</details>

## Report formats

Three ship, and switching between them is a data change:

| Format | For | Photographs |
|---|---|---|
| `buyer_summary` | the buyer, as the primary document | yes |
| `full_technical` | a specialist quoting remedial work | yes |
| `repair_priority` | a working list for collecting quotes | no, by design |

A format is an HTML document with `{{placeholder}}` values and
`<!-- region:name -->` blocks that repeat per system, per finding and per photograph.
Binding is strict in both directions: a template that asks for something this build does not
supply is an error naming the region, and a placeholder left unfilled is an error rather than
a gap in a document a buyer reads. What the verifier holds a format to is read from the
template — the repair-priority sheet declares no photo region, so photographs are not expected
in it.

The inspection systems, the severity scale and the language rail are all data too, in
`config/`. Adding a seventh system, grading on four levels instead of five, or operating
under a firm's own wording rules is a change to YAML and to nothing else.

## SuperDocs surfaces used

| Surface | How |
|---|---|
| **Upload** | the rendered report goes up as one document |
| **Chat** *(review mode)* | rewrites each field note for a first-time reader, held for approval |
| **Approve** | one call per job, carrying every decision including the rail's refusals |
| **Export** | PDF and DOCX with real options — paper size, margins, filename |
| **Images** | every photograph uploaded and embedded in the document by URL |
| **Templates** | each report format registered for reuse across sessions |

Built on the REST API. The brief treats REST and MCP as interchangeable; REST was the shorter
path to the four calls.

## Calls I made

Where the brief or the API was silent, I made a call and recorded it here.

- **Structure is code; prose is the AI; presentation is the template.** Templates are
  *AI-referenced* rather than programmatically applied — there is no "apply template X"
  endpoint — so letting a model own structure would have forfeited the exact guarantee the
  brief asks me to confirm. The AI is given the job it is reliable at and nothing more.
- **We author the report formats.** The premise is that existing inspection reports are not
  formatted for a first-time buyer, so binding data into a firm's existing report would
  reproduce the problem. A builder that requires you to supply the format is a mail-merge
  engine.
- **The rail governs generated text, never the inspector's own.** They are the licensed
  professional and the report is theirs. What this system may not do is put certification
  language in their name.
- **Refusing valid work is treated as a failure of equal weight.** Real inspection vocabulary
  is allow-listed, and terms that are honest when tied to the moment of observation are
  checked for that scope rather than banned — "operated normally when tested" passes where
  "operational" does not. An early version of the rail refused the report's own disclaimer
  and the ordinary English verb in "warrants evaluation"; a test now holds every rule to the
  wording it recommends.
- **A photo URL is a capability, not an identifier.** The image endpoint returns a stable
  `url` and a signed `view_url`, but the stable one is readable with no credentials at all
  and does not expire. It is scrubbed from every log line, refused by the model's own
  `__str__`, and HTML export embeds image bytes so no exported file carries one.
- **A system with no findings still gets its section**, and says that nothing was observed.
  Silence about a system reads as "not inspected", which is a different and more dangerous
  claim.
- **An export is not trusted on its word** — see below.

## What it guarantees

- **Grouped, provably.** The verifier opens the finished PDF and DOCX and checks the
  structure against the catalogue, rather than checking the HTML that was sent.
- **Never certifies.** The rail runs on every proposal before approval *and* on the exported
  bytes afterwards.
- **Never silently stale.** Exporting immediately after approving can return the
  **pre-approval** document with a 200 and no warning — I hit this on a live run, where the
  PDF carried the original text and a DOCX of the same session one second later carried the
  approved rewrites. Every approved rewrite is now read back out of the exported file and the
  export is repeated until they are present. Exports cost nothing, which is what makes
  re-reading the right answer.
- **Idempotent where it costs money.** A photograph's identity is the hash of its cleaned
  bytes, so a crash and re-run re-uploads nothing.
- **Untrusted input is treated as such.** Size caps before read, decode-verification rather
  than trusting an extension, EXIF stripped, everything a person typed escaped before it
  becomes markup, and rejection messages that never echo file content.
- **No secret in code, logs or history.** `.env` is git-ignored; only `.env.example` with
  placeholders is tracked.

## Limitations

- **No web interface yet.** The builder is driven by its CLI and its typed API; the browser
  front end an inspector would use on site is the next piece of work, and I would rather ship
  a spine that is proven than a screen that is not.
- **The offline exporter is a stand-in.** With `PROVIDER=fake` the PDF and DOCX are written
  locally so the demo and the tests are real end to end, but their typography is plain. The
  live path uses SuperDocs' own exporter, which is what the formatting is designed around.
- **One report at a time.** There is no multi-property scheduling or job queue.
- **Photographs are not analysed.** Deliberate: asking a model to describe a property from a
  photograph invites exactly the over-claiming this build exists to prevent. The photograph is
  evidence the reader looks at; the claim stays the inspector's.
- **Attribution is what the inspector typed.** Nothing here verifies that a finding is
  correct, and nothing should be read as doing so.

Engineering notes and the full assumption log are in [PROGRESS.md](PROGRESS.md).
