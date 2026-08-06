// Typed client for the Portfonia holdings API.
//
// Types mirror the backend Pydantic schemas in
// backend/app/schemas/holdings.py. Ring 1 will replace this hand-written mirror
// with types generated from the FastAPI OpenAPI schema (see concept design doc
// section 10, frontend constraint 4). Keep these in sync until then.

export type PricingMode = "auto" | "manual";

export interface ParsedRow {
  name: string;
  ticker: string | null;
  fund_code: string | null;
  currency: string;
  shares: number | null;
  avg_cost: number | null;
  current_value: number | null;
  pricing_mode: PricingMode;
  asset_type: string | null;
  broker: string | null;
  account: string | null;
  portfolio: string | null;
  notes: string | null;
  issues: string[];
  confidence: number;
}

export interface IssueRow {
  raw: string;
  reason: string;
}

export interface CurrencySubtotal {
  currency: string;
  cost_basis: number;
  holding_count: number;
}

export interface BrokerGroup {
  broker: string;
  holding_count: number;
  subtotals: CurrencySubtotal[];
}

export interface UploadPreview {
  valid_rows: ParsedRow[];
  issue_rows: IssueRow[];
  broker_groups: BrokerGroup[];
}

export type UploadJobStatus = "pending" | "success" | "failed";

// Poll target for an async holdings-file parse (issue #77): the LLM parse
// runs in a background Celery task instead of inside the request, since it
// can take several sequential attempts and one case observed ~5 minutes —
// too long to safely hold a single HTTP connection open for.
export interface UploadJob {
  id: string;
  status: UploadJobStatus;
  preview: UploadPreview | null;
  error: string | null;
}

export interface HoldingOut {
  id: string;
  name: string;
  ticker: string | null;
  fund_code: string | null;
  currency: string;
  shares: string | null;
  avg_cost: string | null;
  current_value: string | null;
  pricing_mode: string;
  asset_type: string | null;
  broker: string | null;
  account: string | null;
  portfolio: string | null;
  notes: string | null;
  last_manual_update: string | null;
  created_at: string;
  updated_at: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    return JSON.stringify(body.detail ?? body);
  } catch {
    return res.statusText;
  }
}

export async function listHoldings(): Promise<HoldingOut[]> {
  const res = await fetch("/api/holdings", { cache: "no-store" });
  if (!res.ok) throw new ApiError(res.status, await readError(res));
  return res.json() as Promise<HoldingOut[]>;
}

const UPLOAD_POLL_INTERVAL_MS = 2000;

async function startUploadJob(file: File): Promise<UploadJob> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch("/api/holdings/upload", {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw new ApiError(res.status, await readError(res));
  return res.json() as Promise<UploadJob>;
}

async function getUploadJob(jobId: string): Promise<UploadJob> {
  const res = await fetch(`/api/holdings/upload/${jobId}`, { cache: "no-store" });
  if (!res.ok) throw new ApiError(res.status, await readError(res));
  return res.json() as Promise<UploadJob>;
}

// Starts the async parse and polls until it finishes (issue #77). Kept as a
// single `Promise<UploadPreview>` so callers don't need to change: the
// polling is an internal implementation detail replacing what used to be one
// long-held request/response.
export async function uploadHoldings(file: File): Promise<UploadPreview> {
  let job = await startUploadJob(file);
  while (job.status === "pending") {
    await new Promise((resolve) => setTimeout(resolve, UPLOAD_POLL_INTERVAL_MS));
    job = await getUploadJob(job.id);
  }
  if (job.status === "failed") {
    throw new ApiError(500, job.error ?? "Upload parse failed.");
  }
  if (!job.preview) {
    throw new ApiError(500, "Upload job succeeded but returned no preview.");
  }
  return job.preview;
}

export async function confirmHoldings(
  rows: ParsedRow[],
): Promise<HoldingOut[]> {
  const res = await fetch("/api/holdings/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(rows),
  });
  if (!res.ok) throw new ApiError(res.status, await readError(res));
  return res.json() as Promise<HoldingOut[]>;
}

// Export current holdings as a downloadable markdown file (same format as the
// upload template) so the user can edit and re-upload. Returns a Blob.
export async function exportHoldings(): Promise<Blob> {
  const res = await fetch("/api/holdings/export", { cache: "no-store" });
  if (!res.ok) throw new ApiError(res.status, await readError(res));
  return res.blob();
}
