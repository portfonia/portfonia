// Typed client for the Portfonia holdings API.
//
// Types mirror the backend Pydantic schemas in
// backend/app/schemas/holdings.py. Ring 1 will replace this hand-written mirror
// with types generated from the FastAPI OpenAPI schema (see concept design doc
// section 10, frontend constraint 4). Keep these in sync until then.

import { logout } from "@/lib/auth-actions";

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

// A 401 here can now mean the server-side idle timeout (issue #235), not
// just "never logged in" — that can fire with no client-side idle timer
// ever having run (e.g. the tab was closed and reopened). Route it through
// the same logout() Server Action the client timer already uses so /login
// shows the same expired-session banner, instead of leaving the page
// looking authenticated while every fetch quietly 401s. logout() always
// calls redirect(), which always throws — the ApiError below is an
// unreachable fallback, kept only in case that ever stops being true.
async function throwOnHttpError(res: Response): Promise<never> {
  if (res.status === 401) {
    await logout("expired");
  }
  throw new ApiError(res.status, await readError(res));
}

export async function listHoldings(): Promise<HoldingOut[]> {
  const res = await fetch("/api/holdings", { cache: "no-store" });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<HoldingOut[]>;
}

// Poll backoff for uploadHoldings (issue #77 / PR #82 review): poll once
// immediately after POST rather than waiting out a fixed delay first (the
// worker often finishes small files in well under a second), then back off
// toward a steady interval instead of hammering the endpoint.
const UPLOAD_POLL_START_MS = 500;
const UPLOAD_POLL_MAX_MS = 2000;
const UPLOAD_POLL_BACKOFF_FACTOR = 1.5;
// The backend now bounds a stuck job itself: parse_holdings_upload's Celery
// time_limit is pinned to a 45s SLA, and a hard-kill past that resolves the
// row almost immediately (a Task.Request.on_timeout hook, not Celery's
// task_revoked signal — that one doesn't fire for this path), backstopped
// by a sweeper for the rare case even that hook misses (issue #85, PR #88
// review). 120s stays a generous outer bound on top of that for failure
// modes the backend-side fix doesn't cover at all — worker down or broker
// connection lost before the job ever got picked up — not a bound on the
// parse itself.
const UPLOAD_MAX_WAIT_MS = 120_000;

async function startUploadJob(file: File): Promise<UploadJob> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch("/api/holdings/upload", {
    method: "POST",
    body: form,
  });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<UploadJob>;
}

async function getUploadJob(jobId: string): Promise<UploadJob> {
  const res = await fetch(`/api/holdings/upload/${jobId}`, { cache: "no-store" });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<UploadJob>;
}

// Starts the async parse and polls until it finishes (issue #77). Kept as a
// single `Promise<UploadPreview>` so callers don't need to change: the
// polling is an internal implementation detail replacing what used to be one
// long-held request/response.
export async function uploadHoldings(file: File): Promise<UploadPreview> {
  const deadline = Date.now() + UPLOAD_MAX_WAIT_MS;
  let job = await startUploadJob(file);
  let delay = UPLOAD_POLL_START_MS;
  while (job.status === "pending") {
    if (Date.now() > deadline) {
      throw new ApiError(
        504,
        "Upload is taking longer than expected. It may still finish in the background — try refreshing shortly, or re-upload.",
      );
    }
    job = await getUploadJob(job.id);
    if (job.status !== "pending") break;
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * UPLOAD_POLL_BACKOFF_FACTOR, UPLOAD_POLL_MAX_MS);
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
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<HoldingOut[]>;
}

// Export current holdings as a downloadable markdown file (same format as the
// upload template) so the user can edit and re-upload. Returns a Blob.
export async function exportHoldings(): Promise<Blob> {
  const res = await fetch("/api/holdings/export", { cache: "no-store" });
  if (!res.ok) await throwOnHttpError(res);
  return res.blob();
}

// Mirrors backend/app/schemas/questionnaire.py's QuestionnaireIn (issue #129
// checkpoint B6). Every field is a closed enum the backend validates at the
// API boundary (422 on an unrecognized value) — this client type exists so a
// typo here is caught by tsc, not just by the backend at submit time.
export interface Questionnaire {
  asset_scale: "UNDER_100K" | "100K_500K" | "500K_2M" | "OVER_2M";
  markets: ("US" | "HK" | "A-Share" | "Other")[];
  style: "VALUE" | "GROWTH" | "INDEX" | "MIXED";
  horizon: "SHORT" | "MEDIUM" | "LONG";
  risk_appetite: "CONSERVATIVE" | "BALANCED" | "AGGRESSIVE";
  sectors_of_interest: string[];
  objective: "PRESERVATION" | "GROWTH" | "INCOME";
  intel_focus: "MACRO" | "FUNDAMENTALS" | "GEOPOLITICS" | "BALANCED";
}

export interface InvestmentContext {
  questionnaire: Questionnaire;
  questionnaire_version: string;
  free_text: string | null;
  updated_at: string;
}

// Full overwrite (Concept §4.2: re-answering replaces the record wholesale).
// No client-side getInvestmentContext() counterpart exists: the
// /questionnaire page loads its initial context exclusively through
// getInvestmentContextServer() (server-api.ts) — a client-side reader would
// be dead code until an actual client-side caller needs one (PR #212 review
// finding: an unused export trips this repo's "no unused exports" gate).
export async function putInvestmentContext(
  questionnaire: Questionnaire,
  freeText: string | null,
): Promise<InvestmentContext> {
  const res = await fetch("/api/investment-context", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ questionnaire, free_text: freeText }),
  });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<InvestmentContext>;
}

// Mirrors backend/app/schemas/me.py's MeOut (issue #220, full #221 shape —
// see docs/mechanisms/identity-and-auth.md's "GET /me" entry). `/profile`
// (this PR) only reads `email`/`delivery_email`; the other four fields ride
// along on the wire already but have no frontend reader until #221's gap
// card.
export interface Me {
  email: string;
  delivery_email: string | null;
  tos_accepted_at: string | null;
  has_questionnaire: boolean;
  has_holdings: boolean;
  missing: string[];
  // Mirrors backend PendingVerificationOut (issue #262, Profile Page.md
  // §8.2): the caller's own actionable verification rows — "pending" or
  // "undeliverable" only.
  pending_email_verifications: PendingEmailVerification[];
}

export interface PendingEmailVerification {
  id: string;
  purpose: string;
  email: string;
  status: string;
  expires_at: string;
  last_sent_at: string;
}

// Resend the verification email for one of the caller's own pending/
// undeliverable records (issue #262, Profile Page.md §8.3). The response
// id is the NEW record's — resend supersedes the old row — so the caller
// re-fetches GET /me instead of patching the old id locally (§8.4).
export async function resendEmailVerification(id: string): Promise<void> {
  const res = await fetch(`/api/email-verifications/${id}/resend`, {
    method: "POST",
  });
  if (!res.ok) await throwOnHttpError(res);
}
