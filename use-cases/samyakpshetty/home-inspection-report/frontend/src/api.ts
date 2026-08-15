/**
 * The one place the browser talks to anything.
 *
 * Everything goes through our own API. The SuperDocs key is server-side, and photographs are
 * fetched from `/api/photos/{id}` rather than from the URL SuperDocs returns — that URL
 * resolves for anyone holding it, with no credentials and no expiry, so it is never put in a
 * page, a browser history or a referrer header.
 */

export type System = { key: string; name: string; blurb: string };
export type Severity = {
  key: string;
  rank: number;
  label: string;
  description: string;
  colour: string;
};
export type RailRule = { id: string; category: string; why: string; suggest: string };
export type Catalogue = {
  systems: System[];
  severities: Severity[];
  rail_rules: RailRule[];
  formats: string[];
};

export type Photo = { id: string; caption: string; filename: string };
export type Finding = {
  id: string;
  system_key: string;
  severity_key: string;
  location: string;
  observation: string;
  recommendation: string;
  plain_language: string;
  photos: Photo[];
};
export type Inspection = {
  id: string;
  property: { address_line: string; city: string; postcode: string; year_built: number | null };
  inspector: { name: string; licence_number: string; firm_name: string };
  inspected_on: string;
  template_key: string;
  stage: string;
  findings: Finding[];
};
export type InspectionRow = {
  id: string;
  address_line: string;
  city: string;
  inspector_name: string;
  firm_name: string;
  inspected_on: string;
  stage: string;
  template_key: string;
  finding_count: number;
};

export type Breach = { matched: string; category: string; why: string; suggest: string };
export type Proposal = {
  change_id: string;
  before: string;
  after: string;
  rail_clean: boolean;
  breaches: Breach[];
  decision?: string;
  approved?: boolean;
};
export type Check = { name: string; passed: boolean; detail: string };

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    // A dead backend and a failing request are different problems for the person reading
    // the message, so they get different messages.
    throw new ApiError("Cannot reach the server. Is `docker compose up` still running?", 0);
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* a non-JSON error body is still an error */
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  catalogue: () => request<Catalogue>("/api/catalogue"),

  listInspections: () => request<InspectionRow[]>("/api/inspections"),

  getInspection: (id: string) => request<Inspection>(`/api/inspections/${id}`),

  createInspection: (body: {
    property: { address_line: string; city: string; postcode?: string };
    inspector: { name: string; licence_number?: string; firm_name?: string };
    inspected_on: string;
    template_key: string;
  }) => request<{ id: string }>("/api/inspections", json(body)),

  addFinding: (
    id: string,
    body: {
      system_key: string;
      severity_key: string;
      location: string;
      observation: string;
      recommendation: string;
    },
  ) => request<{ id: string }>(`/api/inspections/${id}/findings`, json(body)),

  addPhoto: (findingId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ id: string; stripped_exif: boolean; width: number; height: number }>(
      `/api/findings/${findingId}/photos`,
      { method: "POST", body: form },
    );
  },

  photoUrl: (photoId: string, thumb = true) =>
    `/api/photos/${photoId}${thumb ? "?thumb=true" : ""}`,

  /** Ask the rail about a piece of text, so the inspector sees the verdict while typing. */
  checkPhrasing: (text: string) =>
    request<{ clean: boolean; summary: string; breaches: Breach[] }>(
      "/api/check-phrasing",
      json({ text }),
    ),

  prepare: (id: string, modelTier: string) =>
    request<{ proposals: Proposal[]; ops_charged: number; ops_remaining: number | null }>(
      `/api/inspections/${id}/prepare?model_tier=${encodeURIComponent(modelTier)}`,
      { method: "POST" },
    ),

  proposals: (id: string) =>
    request<{ job_id: string; proposals: Proposal[] }>(`/api/inspections/${id}/proposals`),

  decide: (id: string, approvals: Record<string, boolean>) =>
    request<{ proposals: Proposal[] }>(`/api/inspections/${id}/decisions`, json(approvals)),

  /**
   * Export, and read the verification the server attaches to the response. The checks
   * travel with the file so the UI cannot offer a download without also being able to say
   * what is in it.
   */
  exportReport: async (id: string, fmt: "pdf" | "docx") => {
    const response = await fetch(`/api/inspections/${id}/export?fmt=${fmt}`, { method: "POST" });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const body = (await response.json()) as { detail?: string };
        if (body.detail) detail = body.detail;
      } catch {
        /* keep the status line */
      }
      throw new ApiError(detail, response.status);
    }
    const checks = JSON.parse(response.headers.get("X-Report-Checks") ?? "[]") as Check[];
    const verified = response.headers.get("X-Report-Verified") === "pass";
    return { blob: await response.blob(), checks, verified };
  },
};
