# Engineering notes

The assumption log for this build, and the reasoning behind the calls I made. The README says
what it does; this says why it is shaped that way.

## The three decisions everything else follows from

**Structure is code, prose is the AI, presentation is the template.**

I researched the templates surface before designing around it, and found there is no endpoint
that applies a saved template to a document. Templates are *AI-referenced*: you register one,
and the AI loads it into a session when you ask for it by name. I confirmed this works — I
registered a report format with three sentinel strings in it and asked the AI to draft from
it by name, and all three came back in the drafted document.

That settled the architecture. If the AI owns structure, then "grouped correctly by system" —
the one thing the brief asks me to confirm — becomes something I hope for. So the report is
rendered deterministically from typed data, and the model is given the job it is actually
reliable at: turning field shorthand into sentences a first-time buyer can read. That was a
judgement at first. It is now a measured result — see the experiment below.

**A report format is a Word document.** Not HTML with markers in it. This build exists
because inspection reports are not written for the person reading them, and a format nobody
can open is a format nobody will redesign. So `templates/` holds `.docx` files: letterhead,
the severity legend, the standing preamble, a section per inspection system, the limitations
clause. A firm opens one in Word, changes it, and drops it back in.

The markers are things a person types, not markup: bracketed tokens like `[findings]`,
`[severity label]`, `[observation]`, `[photograph]`. That is not a stylistic choice. The
earlier design marked regions with `<!-- region:system -->`, and **upload strips HTML
comments** — verified, four sent and none returned, in the same request where `class`
attributes survived. A format marked up that way could never come back from the service with
the markers the binder needed, so the templates surface was registered-and-verified rather
than load-bearing. Bracketed text is not markup, so nothing strips it, and the round trip now
completes: the format registers, comes back exactly as saved, and the report is built on what
came back. Delete it from the account and no report can be produced.

**A format also owns how a finding looks.** It carries one worked example — the severity
label and location, what was observed, the recommended next step, the photograph and its
caption — and the binder reads that example *as the per-finding template*, then removes the
section from the finished report. So a firm changes the layout of every finding by editing
one example in Word. What this build still owns is which system a finding goes under and in
what order; the format owns everything about how it reads.

**We ship the formats.** The brief's premise is that inspection reports are not formatted for
someone who has never read one. A builder that asks a firm to upload its existing report and
binds data into it would faithfully reproduce that problem, and a builder that requires you
to supply the format is a mail-merge engine. So three formats ship with the product, a firm
chooses among them, and — because they are Word documents — can then make them their own.

**The language rail is code, not a prompt.** The brief states the language requirement rather
than suggesting it, and a prompt is a request. It is also not hypothetical: on the first live
drafting sample, asked only to draft a report, the AI produced "recommend replacement **for
safety**", "older but **operational**", and "**typical for the home's age**" — a safety
assurance, a functional verdict, and a reassurance no inspector gave. Those three sentences
are now the regression suite.

## The experiment that decided the architecture

The three decisions above were reasoned from how the templates surface behaves. Reasoning is
not evidence, so before committing to them I built the opposite design and measured it: let
SuperDocs' AI assemble the whole report from a registered format, and let the export verifier
be the guarantee instead of the renderer. If the AI groups correctly, the verifier passes and
the deterministic renderer is unnecessary machinery.

It does not, and the way it fails is worth recording.

| What was asked, all on one session, one document, one approval mode | What happened |
|---|---|
| Load a registered `.docx` format into the session | **Works.** Returned exactly as saved — headings, bold, italics, the legend, the standing text, chunk ids assigned. One turn is a *load*, not a draft: it edits nothing |
| Three unrelated replacements, sentinels at char 172, 2,938 and 5,703 of a 5,716-char message | **3/3 applied.** Long messages are not truncated |
| One system, two findings: replace one placeholder paragraph with six new ones (172 → 610 chars) | **Applied**, correctly placed, and the other five sections untouched. Surgical |
| Six systems, eight findings, in one request | **Nothing.** One unrelated deletion applied, one operation charged, and a reply asking for the findings that were in the message |

So the boundary is not message size, not instruction-following, and not the ability to author
new structure. It is the **scope of a single request**. Narrow asks are precise; a
whole-document authoring ask collapses, and says something misleading while it does.

Three things follow, and they are the shape of the build:

- **The report format is a real `.docx`.** This half of the alternative design was right and
  is now proven end to end: a Word document a firm could open, edit and recognise, registered
  with SuperDocs, loaded back with its structure intact. It replaces the HTML formats marked
  up with `<!-- region:system -->` comments, which could never have worked, because upload
  strips HTML comments (finding 6). The bracketed placeholder paragraphs a human writes in
  Word survive the round trip and are what the binding engine anchors on — the marker is now
  something an author can see and type, rather than an invisible comment.
- **Assembly stays deterministic.** Not because the AI grouped things wrongly — it never got
  far enough to group anything — but because the one operation that reliably does this work
  is a narrow, targeted edit, and a report needs the whole structure at once. Doing it
  through the AI would cost one operation per system and put the card's single stated
  requirement at the mercy of the request that failed above.
- **The AI keeps the job it is measurably good at:** rewriting one chunk at a time, which is
  what the product says it is for. That is one operation per report, gated by a human, with
  the rail in front of it.

The export verifier stays exactly as it is. It was written to prove the renderer's output and
it now also stands as the check on anything the AI touched.

## Calls made where the brief or the API was silent

- **The rail governs generated text only, never the inspector's own words.** They are the
  licensed professional and the report is theirs. What this system may not do is put
  certification language in their name.
- **A refused rewrite leaves the original standing** rather than blocking the report. The
  document loses polish, never content.
- **Refusing valid work is a failure of equal weight to permitting a bad claim.** A rail that
  flagged "safety glazing" or "no leaks were observed" would be switched off within a week.
  So real vocabulary is allow-listed first, and terms that are honest when scoped to the
  moment of observation are checked for that scope rather than banned.
- **Every system gets a section, including one with nothing in it**, which says that nothing
  was observed. Silence about a system reads as "not inspected" — a different and more
  dangerous claim.
- **A photo URL is a capability, not an identifier** (see below), so only the stable form is
  stored, it is scrubbed from every log line, and the model's own `__str__` refuses to print
  it.
- **EXIF is stripped from every photograph.** A phone photograph of a house carries the
  house's coordinates, and this document goes to buyers, agents and lenders.
- **Repair pricing is refused by the rail.** A number in the report reads as an estimate the
  inspector is standing behind; that belongs in a contractor's quote.
- **Photographs are not analysed by a model.** Asking one to describe a property from a
  photograph invites exactly the over-claiming this build exists to prevent.
- **`model_tier` is a parameter, never a constant**, as the integration guidance asks. The
  right default for a legal document is not the right default for a quick pass.

## What the API actually does, where it differs from its documentation

Everything here was established by calling the endpoint and reading the response. Each one is
reported to SuperDocs.

- **`export` immediately after `approve` can return the pre-approval document.** The most
  serious of these. On a live run, `approve` returned 200; the PDF exported two seconds later
  carried the original text while a `.docx` of the same session one second after that carried
  the approved rewrites. Re-exporting minutes later returned the approved text in both, so it
  is a timing race rather than a difference between exporters. **Mitigation:** every approved
  rewrite is read back out of the exported bytes and the export is repeated with backoff until
  they are present. Exports cost nothing, which is what makes re-reading the right answer; if
  it never converges the mismatch is logged at ERROR rather than reported as success.
- **An uploaded image is world-readable.** The endpoint returns a stable `url` and a signed
  `view_url` with a 24-hour expiry, which implies access control. Fetching the plain `url`
  with no authorization header at all returns the image. The signature is decorative, and the
  URL is a permanent capability.
- **`class` survives upload; custom `data-*` attributes do not.** So findings are targeted by
  class and never by an id of our own. The fake strips `data-*` the same way, because a fake
  more permissive than the service is how a design comes to depend on something that is not
  there.
- **A completed job hides its document.** `document_html` is `null`; the content is at
  `result.document_changes.updated_html`.
- **`approve` keys on `change_id`, not `chunk_id`,** and closes the job it is called on, so a
  job's decisions are collected and sent once.
- **`/v1/users/me/usage` and `/limits` reject API keys** with a 401 saying they need a signed-in
  user session — while being published under the same bearer scheme as everything else.
  **`GET /v1/users/me/promotions` does accept an API key** and reports the promo bucket's
  `ops_remaining`, so a balance costs nothing to read. An earlier version of these notes
  claimed you had to spend an operation to find out; that was wrong, and it was wrong because
  I had not tried `promotions`.
- **`session_id` on the templates upload does nothing.** The shared request schema documents
  it as loading the template into a session; exporting that session returns "No document
  loaded in this session."
- **HTML comments do not survive an upload.** The parser drops them. Four sent, none
  returned, in the same request where `class` and `{{placeholder}}` both survived. This
  invalidated the first format design and is why formats are Word documents with bracketed
  tokens now.
- **A delete-type change carries `new_html: null`.** Not every proposed edit is a
  replacement, and typing the field as a string made a whole job — already charged for —
  unparseable. Normalised in one place, with `operation` still naming the kind of edit.
- **A whole-document authoring request applies nothing**, and replies asking for the data it
  was given. Narrow edits are surgical; see the experiment above.

## Things I got wrong, and what fixed them

Kept because the fixes are the interesting part.

- **The rail refused the report's own disclaimer.** "…is not a certification, warranty or
  guarantee…" tripped three rules, and "warrants evaluation" — the ordinary English verb —
  tripped a fourth. The first fix listed the allowed phrasings, which would have reopened on
  the next phrasing. The second recognises the verb by what it governs anywhere in the
  sentence, and a test now holds every rule to the wording it recommends.
- **An early `absolute_negative` rule banned the exact sentence its own guidance suggested.**
  One scoped rule now covers the whole class of negative findings rather than a list of nouns
  that goes stale.
- **The verifier located systems by searching the flattened text**, which found their names in
  the "what was inspected" sentence and put every section boundary in the wrong place. It is
  line-oriented now, anchored on headings.
- **It also missed a phrase in the PDF that it found in the .docx**, because a PDF export wraps
  a paragraph across lines. It searches the joined text and maps back to a line.
- **Word stores `Heating &amp; Cooling`,** so a heading plainly present was reported missing.
- **Inserting photographs at full resolution** made an 8.3 MB file out of 57 KB of image data.

## Verification

- 106 tests, none needing an API key. No `unittest.mock`, no patching: a deterministic fake
  that satisfies the same typed protocol as the live client, and real image bytes generated by
  a committed script.
- `make check` is ruff + `ruff format --check` + `mypy --strict` + the suite, in Docker.
- The whole path proven live against the real API: photographs uploaded, report uploaded,
  rewrites proposed and gated, one approve call, PDF and DOCX exported with all photographs
  embedded, and the same verifier run over the live-exported bytes.
- The exported PDF was opened and looked at, not just parsed.

## Not built

- **The browser interface.** An inspector needs it on site, and it is the next piece of work.
  I would rather hand over a spine that is proven than a screen that is not.
- Multi-property scheduling, a job queue, and user accounts beyond a single firm.
