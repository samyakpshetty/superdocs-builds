import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  api,
  type Catalogue,
  type Check,
  type Inspection,
  type InspectionRow,
  type Proposal,
} from "./api";
import { CheckList, Empty, ErrorNote, RailVerdict, SeverityTag, Skeleton } from "./components";

type Screen = "list" | "walk" | "gate" | "export";

export default function App() {
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [screen, setScreen] = useState<Screen>("list");
  const [current, setCurrent] = useState<string | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);

  const load = useCallback(() => {
    setFatal(null);
    api
      .catalogue()
      .then(setCatalogue)
      .catch((e: ApiError) => setFatal(e.message));
  }, []);
  useEffect(load, [load]);

  return (
    <div className="shell">
      <header className="topbar">
        <span className="topbar__title">Inspection report builder</span>
        {current && screen !== "list" && (
          <button
            className="btn--quiet btn--small"
            onClick={() => {
              setCurrent(null);
              setScreen("list");
            }}
          >
            All inspections
          </button>
        )}
        <span className="topbar__meta">Observational reporting · built on SuperDocs</span>
      </header>

      <main className="main">
        {fatal && <ErrorNote error={fatal} onRetry={load} />}
        {!catalogue && !fatal && <Skeleton />}
        {catalogue && !fatal && (
          <>
            {screen === "list" && (
              <InspectionList
                catalogue={catalogue}
                onOpen={(id) => {
                  setCurrent(id);
                  setScreen("walk");
                }}
              />
            )}
            {screen !== "list" && current && (
              <InspectionWorkspace
                key={current}
                id={current}
                screen={screen}
                setScreen={setScreen}
                catalogue={catalogue}
              />
            )}
          </>
        )}
      </main>
    </div>
  );
}

/* ------------------------------------------------------------------ list */

function InspectionList({
  catalogue,
  onOpen,
}: {
  catalogue: Catalogue;
  onOpen: (id: string) => void;
}) {
  const [rows, setRows] = useState<InspectionRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const refresh = useCallback(() => {
    setError(null);
    api
      .listInspections()
      .then(setRows)
      .catch((e: ApiError) => setError(e.message));
  }, []);
  useEffect(refresh, [refresh]);

  return (
    <>
      <div className="section-head">
        <h1>Inspections</h1>
        <span className="section-head__count">{rows ? `${rows.length} on file` : ""}</span>
        <div style={{ marginLeft: "auto" }}>
          <button className="btn--primary" onClick={() => setCreating((v) => !v)}>
            {creating ? "Cancel" : "New inspection"}
          </button>
        </div>
      </div>

      {creating && (
        <NewInspection
          catalogue={catalogue}
          onCreated={(id) => {
            setCreating(false);
            onOpen(id);
          }}
        />
      )}

      {error && <ErrorNote error={error} onRetry={refresh} />}
      {!rows && !error && <Skeleton rows={2} />}
      {rows?.length === 0 && (
        <Empty title="Nothing here yet">
          <p className="hint">
            Start an inspection, then walk the property system by system. Nothing is sent
            anywhere until you ask for the report.
          </p>
        </Empty>
      )}
      {rows && rows.length > 0 && (
        <div className="list">
          {rows.map((r) => (
            <button key={r.id} className="list__item" onClick={() => onOpen(r.id)}>
              <span className="list__main">
                <strong>{r.address_line}</strong>
                <span className="list__meta">
                  {r.city} · {r.inspected_on} · {r.finding_count}{" "}
                  {r.finding_count === 1 ? "finding" : "findings"} · {r.stage}
                </span>
              </span>
              <span aria-hidden="true">›</span>
            </button>
          ))}
        </div>
      )}
    </>
  );
}

function NewInspection({
  catalogue,
  onCreated,
}: {
  catalogue: Catalogue;
  onCreated: (id: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const today = new Date().toISOString().slice(0, 10);

  return (
    <form
      className="card stack"
      onSubmit={(e) => {
        e.preventDefault();
        const f = new FormData(e.currentTarget);
        setBusy(true);
        setError(null);
        api
          .createInspection({
            property: {
              address_line: String(f.get("address") ?? ""),
              city: String(f.get("city") ?? ""),
              postcode: String(f.get("postcode") ?? ""),
            },
            inspector: {
              name: String(f.get("inspector") ?? ""),
              licence_number: String(f.get("licence") ?? ""),
              firm_name: String(f.get("firm") ?? ""),
            },
            inspected_on: String(f.get("date") ?? today),
            template_key: String(f.get("format") ?? catalogue.formats[0]),
          })
          .then((r) => onCreated(r.id))
          .catch((e: ApiError) => setError(e.message))
          .finally(() => setBusy(false));
      }}
    >
      <h3>New inspection</h3>
      {error && <ErrorNote error={error} />}
      <div className="field-grid field-grid--2">
        <label>
          Property address
          <input name="address" required autoComplete="off" />
        </label>
        <label>
          Town or city
          <input name="city" required autoComplete="off" />
        </label>
        <label>
          Postcode
          <input name="postcode" autoComplete="off" />
        </label>
        <label>
          Date of inspection
          <input name="date" type="date" defaultValue={today} required />
        </label>
        <label>
          Inspector
          <input name="inspector" required autoComplete="off" />
        </label>
        <label>
          Licence number
          <input name="licence" autoComplete="off" />
        </label>
        <label>
          Firm
          <input name="firm" autoComplete="off" />
        </label>
        <label>
          Report format
          <select name="format" defaultValue={catalogue.formats[0]}>
            {catalogue.formats.map((f) => (
              <option key={f} value={f}>
                {f.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="row">
        <button className="btn--primary" type="submit" disabled={busy}>
          {busy ? "Creating…" : "Start inspection"}
        </button>
      </div>
    </form>
  );
}

/* ------------------------------------------------------------- workspace */

function InspectionWorkspace({
  id,
  screen,
  setScreen,
  catalogue,
}: {
  id: string;
  screen: Screen;
  setScreen: (s: Screen) => void;
  catalogue: Catalogue;
}) {
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    setError(null);
    return api
      .getInspection(id)
      .then(setInspection)
      .catch((e: ApiError) => setError(e.message));
  }, [id]);
  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (error) return <ErrorNote error={error} onRetry={() => void refresh()} />;
  if (!inspection) return <Skeleton />;

  return (
    <>
      <div className="section-head">
        <h1>{inspection.property.address_line}</h1>
        <span className="section-head__count">
          {inspection.property.city} · {inspection.inspected_on}
        </span>
      </div>

      <div className="tabs" role="tablist">
        {(
          [
            ["walk", "The walk"],
            ["gate", "Review"],
            ["export", "Export"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            role="tab"
            className="tab"
            aria-selected={screen === key}
            onClick={() => setScreen(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {screen === "walk" && (
        <Walk inspection={inspection} catalogue={catalogue} onChanged={() => void refresh()} />
      )}
      {screen === "gate" && (
        <Gate id={id} catalogue={catalogue} onDecided={() => void refresh()} />
      )}
      {screen === "export" && <ExportPanel id={id} inspection={inspection} />}
    </>
  );
}

/* ------------------------------------------------------------------ walk */

function Walk({
  inspection,
  catalogue,
  onChanged,
}: {
  inspection: Inspection;
  catalogue: Catalogue;
  onChanged: () => void;
}) {
  const severityByKey = useMemo(
    () => new Map(catalogue.severities.map((s) => [s.key, s])),
    [catalogue.severities],
  );
  const [openSystem, setOpenSystem] = useState<string | null>(null);

  return (
    <div className="stack">
      <p className="hint">
        Findings are grouped by system in the report, in this order, whatever order you record
        them in.
      </p>
      {catalogue.systems.map((system) => {
        const findings = inspection.findings.filter((f) => f.system_key === system.key);
        return (
          <section className="card stack" key={system.key}>
            <div className="section-head">
              <h3>{system.name}</h3>
              <span className="section-head__count">
                {findings.length === 0
                  ? "nothing recorded"
                  : `${findings.length} recorded`}
              </span>
              <div style={{ marginLeft: "auto" }}>
                <button
                  className="btn--small"
                  onClick={() => setOpenSystem(openSystem === system.key ? null : system.key)}
                >
                  {openSystem === system.key ? "Close" : "Add finding"}
                </button>
              </div>
            </div>

            {findings.map((f) => (
              <FindingCard
                key={f.id}
                finding={f}
                severity={severityByKey.get(f.severity_key)}
                onChanged={onChanged}
              />
            ))}

            {openSystem === system.key && (
              <NewFinding
                inspectionId={inspection.id}
                systemKey={system.key}
                catalogue={catalogue}
                onAdded={() => {
                  setOpenSystem(null);
                  onChanged();
                }}
              />
            )}
          </section>
        );
      })}
    </div>
  );
}

function FindingCard({
  finding,
  severity,
  onChanged,
}: {
  finding: Inspection["findings"][number];
  severity: Catalogue["severities"][number] | undefined;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  return (
    <div className="card card--flat stack" style={{ background: "var(--surface)" }}>
      <div className="row">
        <SeverityTag severity={severity} />
        {finding.location && <strong>{finding.location}</strong>}
      </div>
      <p>{finding.observation}</p>
      {finding.plain_language && (
        <div className="notice notice--ok">
          <div className="notice__head">Approved rewrite, used in the report</div>
          <div>{finding.plain_language}</div>
        </div>
      )}
      {finding.recommendation && (
        <p className="hint">Recommended next step: {finding.recommendation}</p>
      )}

      {finding.photos.length > 0 && (
        <div className="thumbs">
          {finding.photos.map((p) => (
            <img
              key={p.id}
              className="thumb"
              src={api.photoUrl(p.id)}
              alt={p.caption || `Photograph for ${finding.location || "this finding"}`}
              loading="lazy"
            />
          ))}
        </div>
      )}

      {error && <ErrorNote error={error} />}
      <div className="row">
        <input
          ref={fileRef}
          type="file"
          accept="image/png,image/jpeg,image/webp"
          className="visually-hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (!file) return;
            setBusy(true);
            setError(null);
            api
              .addPhoto(finding.id, file)
              .then(onChanged)
              .catch((err: ApiError) => setError(err.message))
              .finally(() => {
                setBusy(false);
                if (fileRef.current) fileRef.current.value = "";
              });
          }}
        />
        <button className="btn--small" disabled={busy} onClick={() => fileRef.current?.click()}>
          {busy ? "Processing…" : "Add photograph"}
        </button>
        <span className="hint">Location data is removed before the photo is stored.</span>
      </div>
    </div>
  );
}

function NewFinding({
  inspectionId,
  systemKey,
  catalogue,
  onAdded,
}: {
  inspectionId: string;
  systemKey: string;
  catalogue: Catalogue;
  onAdded: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [verdict, setVerdict] = useState<{ clean: boolean; summary: string } | null>(null);

  // The rail's opinion while you type. Advisory here — an inspector may write whatever they
  // judge correct, and the rail only ever *blocks* text this system generated.
  useEffect(() => {
    if (note.trim().length < 12) {
      setVerdict(null);
      return;
    }
    const t = setTimeout(() => {
      api
        .checkPhrasing(note)
        .then((v) => setVerdict({ clean: v.clean, summary: v.summary }))
        .catch(() => setVerdict(null));
    }, 400);
    return () => clearTimeout(t);
  }, [note]);

  return (
    <form
      className="card stack"
      onSubmit={(e) => {
        e.preventDefault();
        const f = new FormData(e.currentTarget);
        setBusy(true);
        setError(null);
        api
          .addFinding(inspectionId, {
            system_key: systemKey,
            severity_key: String(f.get("severity") ?? ""),
            location: String(f.get("location") ?? ""),
            observation: note,
            recommendation: String(f.get("recommendation") ?? ""),
          })
          .then(onAdded)
          .catch((err: ApiError) => setError(err.message))
          .finally(() => setBusy(false));
      }}
    >
      {error && <ErrorNote error={error} />}
      <div className="field-grid field-grid--2">
        <label>
          Severity
          <select name="severity" defaultValue={catalogue.severities[1]?.key} required>
            {catalogue.severities.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Location
          <input name="location" placeholder="North chimney" autoComplete="off" />
        </label>
      </div>
      <label>
        What you observed
        <textarea
          name="observation"
          required
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="S flashing gap ~2in @ chimney, staining below"
        />
      </label>
      {verdict && !verdict.clean && (
        <div className="notice">
          <div className="notice__head">Heads up — {verdict.summary}</div>
          <div className="hint">
            Your wording is kept exactly as written. This is only a note that the phrasing
            reads as a claim about the property rather than an observation.
          </div>
        </div>
      )}
      <label>
        Recommended next step
        <input
          name="recommendation"
          placeholder="Evaluation by a licensed roofing contractor."
          autoComplete="off"
        />
      </label>
      <div className="row">
        <button className="btn--primary" type="submit" disabled={busy || !note.trim()}>
          {busy ? "Saving…" : "Record finding"}
        </button>
      </div>
    </form>
  );
}

/* ------------------------------------------------------------------ gate */

function Gate({
  id,
  catalogue,
  onDecided,
}: {
  id: string;
  catalogue: Catalogue;
  onDecided: () => void;
}) {
  const [proposals, setProposals] = useState<Proposal[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<Record<string, boolean>>({});
  const [tier, setTier] = useState("core");
  const [ops, setOps] = useState<number | null>(null);

  const load = useCallback(() => {
    setError(null);
    api
      .proposals(id)
      .then((r) => setProposals(r.proposals))
      .catch((e: ApiError) => setError(e.message));
  }, [id]);
  useEffect(load, [load]);

  const undecided = (proposals ?? []).filter((p) => p.rail_clean && p.decision === "pending");
  const allDecided = (proposals ?? []).length > 0 && undecided.length === 0;

  return (
    <div className="stack">
      <div className="card stack">
        <h3>Plain-language rewrite</h3>
        <p className="hint">
          SuperDocs rewrites each field note for someone who has never read an inspection
          report. Nothing is applied until you decide, and anything that reads as a
          certification is refused before it reaches you.
        </p>
        <div className="row">
          <label style={{ maxWidth: 220 }}>
            Precision
            <select value={tier} onChange={(e) => setTier(e.target.value)}>
              <option value="core">Standard</option>
              <option value="turbo">Fastest</option>
              <option value="pro">Careful</option>
              <option value="max">Most careful</option>
            </select>
          </label>
          <button
            className="btn--primary"
            disabled={busy !== null}
            onClick={() => {
              setBusy("prepare");
              setError(null);
              api
                .prepare(id, tier)
                .then((r) => {
                  setProposals(r.proposals);
                  setOps(r.ops_remaining);
                  setApprovals({});
                })
                .catch((e: ApiError) => setError(e.message))
                .finally(() => setBusy(null));
            }}
          >
            {busy === "prepare" ? "Asking…" : "Propose rewrites"}
          </button>
          {ops !== null && <span className="hint">{ops.toLocaleString()} operations left</span>}
        </div>
        {busy === "prepare" && (
          <p className="hint">
            A large report can take a minute or more. Leaving this page is safe — the review
            waits for you.
          </p>
        )}
      </div>

      {error && <ErrorNote error={error} onRetry={load} />}
      {busy === "prepare" && <Skeleton rows={2} />}
      {proposals?.length === 0 && !busy && (
        <Empty title="Nothing proposed yet">
          <p className="hint">Record your findings, then ask for the rewrites.</p>
        </Empty>
      )}

      {proposals?.map((p) => (
        <article className="card stack" key={p.change_id}>
          <div className="diff">
            <div className="diff__side">
              <span className="diff__label">What you wrote</span>
              <div className="diff__text">{p.before}</div>
            </div>
            <div className="diff__side">
              <span className="diff__label">Proposed for the buyer</span>
              <div className="diff__text">{p.after}</div>
            </div>
          </div>
          <RailVerdict clean={p.rail_clean} breaches={p.breaches ?? []} />
          {p.rail_clean && (
            <div className="row">
              <button
                className={approvals[p.change_id] === true ? "btn--primary" : ""}
                aria-pressed={approvals[p.change_id] === true}
                onClick={() => setApprovals((a) => ({ ...a, [p.change_id]: true }))}
              >
                Use the rewrite
              </button>
              <button
                className={approvals[p.change_id] === false ? "btn--primary" : ""}
                aria-pressed={approvals[p.change_id] === false}
                onClick={() => setApprovals((a) => ({ ...a, [p.change_id]: false }))}
              >
                Keep my wording
              </button>
              {p.decision && p.decision !== "pending" && (
                <span className="hint">already {p.decision}</span>
            )}
            </div>
          )}
        </article>
      ))}

      {proposals && proposals.length > 0 && (
        <div className="card stack">
          <p className="hint">
            Decisions are sent together, because approving closes the round on SuperDocs&rsquo;
            side and it cannot be reopened.
          </p>
          <div className="row">
            <button
              className="btn--primary"
              disabled={
                busy !== null ||
                allDecided ||
                undecided.some((p) => approvals[p.change_id] === undefined)
              }
              onClick={() => {
                setBusy("decide");
                setError(null);
                api
                  .decide(id, approvals)
                  .then((r) => {
                    setProposals(r.proposals);
                    onDecided();
                  })
                  .catch((e: ApiError) => setError(e.message))
                  .finally(() => setBusy(null));
              }}
            >
              {busy === "decide" ? "Sending…" : "Apply decisions"}
            </button>
            {undecided.length > 0 && (
              <span className="hint">
                {undecided.filter((p) => approvals[p.change_id] === undefined).length} still to
                decide
              </span>
            )}
            {allDecided && <span className="hint">All decided — ready to export.</span>}
          </div>
        </div>
      )}

      <details className="card">
        <summary>What the language rail refuses ({catalogue.rail_rules.length} rules)</summary>
        <div className="stack" style={{ marginTop: "0.7rem" }}>
          {catalogue.rail_rules.map((r) => (
            <div key={r.id}>
              <strong>{r.category}</strong> — {r.why} <em>{r.suggest}</em>
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}

/* ---------------------------------------------------------------- export */

function ExportPanel({ id, inspection }: { id: string; inspection: Inspection }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checks, setChecks] = useState<Check[] | null>(null);
  const [verified, setVerified] = useState<boolean | null>(null);

  const run = (fmt: "pdf" | "docx") => {
    setBusy(fmt);
    setError(null);
    api
      .exportReport(id, fmt)
      .then(({ blob, checks, verified }) => {
        setChecks(checks);
        setVerified(verified);
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${inspection.property.address_line.replace(/\W+/g, "-").toLowerCase()}.${fmt}`;
        a.click();
        URL.revokeObjectURL(url);
      })
      .catch((e: ApiError) => setError(e.message))
      .finally(() => setBusy(null));
  };

  return (
    <div className="stack">
      <div className="card stack">
        <h3>Export the report</h3>
        <p className="hint">
          Findings are grouped by inspection system, most urgent first within each. Photographs
          are embedded in the file itself, so the report carries its own evidence.
        </p>
        <div className="row">
          <button className="btn--primary" disabled={busy !== null} onClick={() => run("pdf")}>
            {busy === "pdf" ? "Building…" : "Download PDF"}
          </button>
          <button disabled={busy !== null} onClick={() => run("docx")}>
            {busy === "docx" ? "Building…" : "Download Word"}
          </button>
        </div>
      </div>

      {error && <ErrorNote error={error} />}
      {busy && <Skeleton rows={1} />}

      {checks && (
        <div className="card stack">
          <div className="section-head">
            <h3>What the exported file actually contains</h3>
            <span className="section-head__count">
              {verified ? "all checks passed" : "some checks failed"}
            </span>
          </div>
          <p className="hint">
            These are read back out of the finished file, not out of what was sent to build it.
          </p>
          <CheckList checks={checks} />
        </div>
      )}
    </div>
  );
}
