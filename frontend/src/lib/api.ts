// Typed client for the Portfonia holdings API.
//
// Types mirror the backend Pydantic schemas in
// backend/app/schemas/holdings.py. Ring 1 will replace this hand-written mirror
// with types generated from the FastAPI OpenAPI schema (see concept design doc
// section 10, frontend constraint 4). Keep these in sync until then.

import type { BaseCurrency } from "@/app/portfolio/_components/currencies";
import { logout } from "@/lib/auth-actions";
import { filenameFromContentDisposition } from "@/lib/template";

export type PricingMode = "auto" | "manual";
export type AssetType = "stock" | "etf" | "fund" | "cash" | "wmf" | "other";
// Widened from 4 to the backend's full 8-value instrument Market taxonomy
// (issue #57 stage 57-3 frozen design section 7) — UK/Europe/Japan/Korea
// were already reachable through the holdings form's `as ParsedRow["market"]`
// cast (see holding-form.tsx) despite this type omitting them, so this is a
// type-accuracy fix, not a behavior change. Distinct from `Questionnaire.
// markets` below, which mirrors the separate, still-4-value `VALID_MARKETS`
// investment-style taxonomy (`questionnaire_taxonomy.py`) and is out of
// scope for #57.
export type Market = "US" | "HK" | "A-Share" | "UK" | "Europe" | "Japan" | "Korea" | "Other";
// Issue #421: independent of real position size, drives a config-driven
// floor on §3 depth. null = not watched.
export type WatchTier = "watch" | "focus" | "critical";
export const WATCH_TIERS: readonly WatchTier[] = ["watch", "focus", "critical"];
export type ConfirmMode = "append" | "replace";
export type IssueSeverity = "info" | "warning";

export interface IssueNote {
  code: string;
  params: Record<string, string>;
  severity: IssueSeverity;
}

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
  asset_class?: string;
  market?: Market | null;
  broker: string | null;
  account: string | null;
  portfolio: string | null;
  notes: string | null;
  watch_tier?: WatchTier | null;
  issues: IssueNote[];
  confidence: number;
  capture_supported: boolean;
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
  unsupported_capture_count: number;
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
  capture_supported: boolean;
  broker: string | null;
  account: string | null;
  portfolio: string | null;
  notes: string | null;
  watch_tier?: WatchTier | null;
  last_manual_update: string | null;
  created_at: string;
  updated_at: string;
  asset_class?: string;
  market?: string | null;
  position?: number | null;
}

export type HoldingPatch = Partial<{
  name: string;
  ticker: string | null;
  fund_code: string | null;
  currency: string;
  shares: number | null;
  avg_cost: number | null;
  current_value: number | null;
  pricing_mode: PricingMode;
  asset_type: string | null;
  market: Market | null;
  broker: string | null;
  account: string | null;
  portfolio: string | null;
  notes: string | null;
  watch_tier: WatchTier | null;
}>;

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
  mode: ConfirmMode,
): Promise<HoldingOut[]> {
  const res = await fetch(`/api/holdings/confirm?mode=${mode}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(rows),
  });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<HoldingOut[]>;
}

export async function createHolding(row: ParsedRow): Promise<HoldingOut> {
  const res = await fetch("/api/holdings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(row),
  });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<HoldingOut>;
}

export async function updateHolding(
  id: string,
  patch: HoldingPatch,
): Promise<HoldingOut> {
  const res = await fetch(`/api/holdings/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<HoldingOut>;
}

export async function deleteHolding(id: string): Promise<void> {
  const res = await fetch(`/api/holdings/${id}`, { method: "DELETE" });
  if (!res.ok) await throwOnHttpError(res);
}

export async function reorderHoldings(ids: string[]): Promise<HoldingOut[]> {
  const res = await fetch("/api/holdings/reorder", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<HoldingOut[]>;
}

// `locale` is an export code (en / zh / zh-Hant): callers map the UI
// locale zh-Hans to zh and preserve zh-Hant, taking precedence over the
// report-language fallback (issue #319 item 9). Omit to keep the old
// report-language behavior.
export async function downloadHoldingsTemplate(locale?: string): Promise<Blob> {
  const qs = locale ? `?locale=${encodeURIComponent(locale)}` : "";
  const res = await fetch(`/api/holdings/template${qs}`, { cache: "no-store" });
  if (!res.ok) await throwOnHttpError(res);
  return res.blob();
}

// Export current holdings as a downloadable markdown file (same format as the
// upload template) so the user can edit and re-upload. Returns a Blob.
export async function exportHoldings(
  locale?: string,
): Promise<{ blob: Blob; filename: string }> {
  const qs = locale ? `?locale=${encodeURIComponent(locale)}` : "";
  const res = await fetch(`/api/holdings/export${qs}`, { cache: "no-store" });
  if (!res.ok) await throwOnHttpError(res);
  const filename = filenameFromContentDisposition(
    res.headers.get("Content-Disposition"),
    "holdings.md",
  );
  return { blob: await res.blob(), filename };
}

// Mirrors backend/app/schemas/portfolio.py's HoldingValueOut /
// PortfolioSummaryResponse (issue #320, C2 dashboard). Decimal fields arrive
// as strings, same convention as HoldingOut above.
export interface HoldingValueOut {
  holding_id: string;
  name: string;
  ticker: string | null;
  fund_code: string | null;
  currency: string;
  asset_type: string | null;
  asset_class: string | null;
  market: string;
  market_value: string | null;
  market_value_base: string | null;
  price_as_of: string | null;
  pricing_mode: string;
  capture_supported: boolean;
  broker: string | null;
  account: string | null;
  portfolio: string | null;
  watch_tier: WatchTier | null;
  avg_cost: string | null;
  shares: string | null;
  notes: string | null;
  cost_basis_base: string | null;
  unrealized_pnl_base: string | null;
  unrealized_pnl_pct: string | null;
}

export interface PortfolioSummary {
  base_currency: string;
  // Per-currency (never "USD") rate_date actually used for this render's
  // conversions (issue #354) — replaces the old single fx_date scalar,
  // which assumed every FX pair shared one date. Empty when the book is
  // single-currency and already matches base_currency (no conversion
  // happened).
  fx_rates_as_of: Record<string, string>;
  // Currency codes whose resolved FX row was fetched more than 48 hours
  // ago (issue #519). Non-null. The rate is still used for valuation;
  // this list is disclosure only and is not recomputed on the client.
  stale_fx_pairs: string[];
  total_base: string;
  by_market: Record<string, string>;
  by_currency: Record<string, string>;
  by_asset_type: Record<string, string>;
  by_asset_class: Record<string, string>;
  by_group: Record<string, string>;
  by_broker: Record<string, string>;
  by_account: Record<string, string>;
  total_cost_basis_base: string;
  total_unrealized_pnl_base: string;
  total_unrealized_pnl_pct: string | null;
  price_as_of_date: string | null;
  stale_tickers: string[];
  holdings: HoldingValueOut[];
}

export async function getPortfolioSummary(baseCurrency: string): Promise<PortfolioSummary> {
  const res = await fetch(
    `/api/portfolio/summary?base_currency=${encodeURIComponent(baseCurrency)}`,
    { cache: "no-store" },
  );
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<PortfolioSummary>;
}

// issue #202: explicit "Send holdings overview" button on /portfolio.
// `sent: false` + `retry_after_seconds` is the routine 15-minute-cooldown
// case, not an error — the caller renders "still N minutes left", not a
// failure message.
export interface SendOverviewResponse {
  sent: boolean;
  retry_after_seconds: number | null;
}

export async function sendPortfolioOverview(baseCurrency: string): Promise<SendOverviewResponse> {
  const res = await fetch(
    `/api/portfolio/send-overview?base_currency=${encodeURIComponent(baseCurrency)}`,
    { method: "POST", cache: "no-store" },
  );
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<SendOverviewResponse>;
}

export type PortfolioExportFormat = "xlsx" | "md";

// GET /portfolio/export (issue #331) — the computed, priced snapshot
// (per-holding rows + as-of/base-currency header), distinct from
// exportHoldings() above (declared, unpriced fields for re-import).
// `locale` follows the same override-users.locale precedence as
// exportHoldings/downloadHoldingsTemplate (issue #319 item 9).
export async function exportPortfolio(
  format: PortfolioExportFormat,
  baseCurrency: string,
  locale?: string,
): Promise<{ blob: Blob; filename: string }> {
  const params = new URLSearchParams({ format, base_currency: baseCurrency });
  if (locale) params.set("locale", locale);
  const res = await fetch(`/api/portfolio/export?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) await throwOnHttpError(res);
  const filename = filenameFromContentDisposition(
    res.headers.get("Content-Disposition"),
    `portfolio.${format}`,
  );
  return { blob: await res.blob(), filename };
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
// see docs/mechanisms/identity-and-auth.md's "GET /me" entry). The Profile
// page reads email/delivery_email, the verification timestamps (issue
// #269), `missing` (gap card), and `pending_email_verifications`.
export type BriefingPlan = "weekly" | "mwf" | "daily";
export type SubscriptionType = BriefingPlan | "jade";

export interface Subscription {
  cadence: BriefingPlan | "none";
  status: "active" | "inactive" | "expired" | "cancelled";
  type: SubscriptionType | null;
  expires_on: string | null;
  cancel_pending: boolean;
  next_adjustment_at: string | null;
}

export interface SubscriptionQuote {
  cadence: BriefingPlan;
  action: "subscribe" | "change" | "resume" | "none";
  type: SubscriptionType;
  fee: string;
  returned: string;
  balance: string;
  balance_after: string;
  sufficient: boolean;
  period_start: string | null;
  expires_on: string | null;
  first_report_at: string;
  needs_holdings: boolean;
  blocked: "daily_limit" | "email_unverified" | null;
}

export async function getSubscriptionQuote(type: SubscriptionType): Promise<SubscriptionQuote> {
  const res = await fetch(`/api/me/subscription/quote?type=${type}`, { cache: "no-store" });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<SubscriptionQuote>;
}

export async function setSubscription(type: SubscriptionType): Promise<Subscription> {
  const res = await fetch("/api/me/subscription", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type }),
  });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<Subscription>;
}

export async function cancelSubscription(): Promise<Subscription> {
  const res = await fetch("/api/me/subscription/cancel", { method: "POST" });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<Subscription>;
}

export interface Me {
  subscription: Subscription;
  email: string;
  credit_balance: string;
  delivery_email: string | null;
  // Issue #269: raw verification timestamps mirroring the `users` columns,
  // so the Profile page derives verification state without a new endpoint.
  // Both null = no verified receiving address.
  email_verified_at: string | null;
  delivery_email_verified_at: string | null;
  tos_accepted_at: string | null;
  has_questionnaire: boolean;
  has_holdings: boolean;
  missing: string[];
  // Mirrors backend PendingVerificationOut (issue #262, Profile Page.md
  // §8.2): the caller's own actionable verification rows — "pending" or
  // "undeliverable" only.
  pending_email_verifications: PendingEmailVerification[];
  // Issue #308: sourced from users.locale — deliberately named apart from
  // the frontend's own Locale/LOCALES UI-chrome type (issue #209), see
  // backend/app/schemas/me.py's MeOut docstring for why.
  report_language: string;
  // Issue #350 item 1: sourced from users.base_currency — the report-
  // currency sibling of report_language above.
  report_currency: string;
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

// Start a NEW verification for one of the caller's own known email fields
// (issue #289, Profile Page.md §10): purpose=account_email resolves
// users.email, purpose=delivery_email resolves users.delivery_email — the
// server never accepts an arbitrary address from the client. Unlike resend,
// this requires no existing pending/undeliverable record: it is the
// self-service recovery path after the only verified address was revoked.
// The caller re-fetches GET /me via router.refresh() on success.
export async function createEmailVerification(
  purpose: "account_email" | "delivery_email",
): Promise<void> {
  const res = await fetch("/api/email-verifications", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ purpose }),
  });
  if (!res.ok) await throwOnHttpError(res);
}

// Set the caller's own report language (issue #308, Profile page's new
// Report Language control). Saves immediately on change — no separate Save
// button, matching this page's other live controls — and the caller
// re-fetches GET /me via router.refresh() on success, same discipline as
// resendEmailVerification/createEmailVerification above.
export async function updateReportLanguage(reportLanguage: "en" | "zh" | "zh-Hant"): Promise<void> {
  const res = await fetch("/api/me/report-language", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ report_language: reportLanguage }),
  });
  if (!res.ok) await throwOnHttpError(res);
}

// Set the caller's own report currency (issue #350 item 1, Profile page's
// new Report Currency control) — same save-immediately discipline as
// updateReportLanguage above.
export async function updateReportCurrency(reportCurrency: string): Promise<void> {
  const res = await fetch("/api/me/report-currency", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ report_currency: reportCurrency }),
  });
  if (!res.ok) await throwOnHttpError(res);
}

// Mirrors backend/app/schemas/portfolio.py's Performance*Out models (issue
// #360 Phase 1 — the frozen GET /portfolio/performance contract Phase 2
// builds against). Decimal fields arrive as strings, same convention as the
// rest of this file. `return_pct_cumulative`/`value_change_pct` are ratios
// (0.0234 = 2.34%), matching total_unrealized_pnl_pct's convention.
export type PerformanceRange = "1M" | "6M" | "YTD" | "1Y" | "5Y" | "ALL";
export type BenchmarkCode = "sp500" | "dow30" | "nasdaq" | "csi300";

export interface RiskVolSeries {
  status: "ok" | "insufficient_sample";
  current: string | null;
  tier: "low" | "medium" | "high" | null;
  window_start: string | null;
  window_end: string | null;
  sample_count: number;
  points: { date: string; vol: string }[];
}

export interface PortfolioRiskResponse {
  base_currency: string;
  portfolio_vol: RiskVolSeries;
  benchmark_vols: (RiskVolSeries & { code: BenchmarkCode })[];
  beta: { status: "ok" | "insufficient_sample"; value: string | null; sample_count: number };
  risk: { status: "ok" | "no_questionnaire" | "insufficient_sample" | "data_quality"; label: "within" | "caution" | "exceeds" | null };
  deviation: { status: "ok" | "no_questionnaire" | "no_valued_holdings"; delta: number | null };
  manual_valuation_share: string | null;
}

export async function getPortfolioRisk(
  benchmarks: BenchmarkCode[],
  baseCurrency: string,
): Promise<PortfolioRiskResponse> {
  const params = new URLSearchParams({ base_currency: baseCurrency });
  benchmarks.forEach((code) => params.append("benchmarks", code));
  const res = await fetch(`/api/portfolio/risk?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<PortfolioRiskResponse>;
}

export const PERFORMANCE_RANGES = ["1M", "6M", "YTD", "1Y", "5Y", "ALL"] as const satisfies readonly PerformanceRange[];
export const BENCHMARK_CODES = ["sp500", "dow30", "nasdaq", "csi300"] as const satisfies readonly BenchmarkCode[];
// Issue #382 first-visit UI state. The GET handler still defaults `range`
// to "1Y" when the param is omitted; the page always sends these values.
export const DEFAULT_PERFORMANCE_RANGE: PerformanceRange = "1M";
export const DEFAULT_BENCHMARKS: readonly BenchmarkCode[] = ["sp500"];

export interface PerformancePoint {
  date: string;
  value_base: string;
  return_pct_cumulative: string;
  is_approximate: boolean;
}

export interface PortfolioPerformanceSeries {
  empty: boolean;
  start_date: string | null;
  end_date: string | null;
  // First real (non-backfilled) complete-batch snapshot day for this user,
  // unfiltered by dimension (issue #366) — may be earlier than start_date
  // when dimension filters shorten the series.
  tracking_start: string | null;
  points: PerformancePoint[];
  quality_flags: string[];
}

export type BenchmarkUnavailableReason =
  | "missing_price"
  | "stale_price"
  | "invalid_price"
  | "missing_fx"
  | "stale_fx"
  | "invalid_fx";

export type BenchmarkNormalization = "portfolio_start" | "own_start" | "unavailable";

export type BenchmarkComparisonStatus =
  | "available"
  | "baseline_only"
  | "no_portfolio"
  | "anchor_unavailable"
  | "incomplete_window";

export interface BenchmarkPoint {
  date: string;
  return_pct_cumulative: string | null;
  price_as_of: string | null;
  fx_as_of: Record<string, string>;
  carried: boolean;
  unavailable_reason: BenchmarkUnavailableReason | null;
}

export interface BenchmarkPerformanceSeries {
  index_code: BenchmarkCode;
  name: string;
  start_date: string | null;
  points: BenchmarkPoint[];
  // Comparison eligibility (issue #377). Display uses `displayable`, not this.
  comparable: boolean;
  displayable: boolean;
  normalization: BenchmarkNormalization;
  anchor_date: string | null;
  display_start_date: string | null;
  display_end_date: string | null;
  comparison_start: string | null;
  comparison_end: string | null;
  comparison_status: BenchmarkComparisonStatus;
  comparison_return_pct: string | null;
}

export interface PortfolioPerformanceHeader {
  value_base: string;
  value_change_base: string;
  value_change_pct: string;
  // "market_value_change" when twr=true (issue #360 requirement 7) — the $
  // figure is never return dollars; the UI labels it as such.
  label: string;
}

export interface PortfolioPerformanceMeta {
  range: PerformanceRange;
  twr: boolean;
  base_currency: string;
  filters: Record<string, string[]>;
}

// Issue #433: asset-class allocation history. `weights` only ever carries
// closed-taxonomy keys with a usable, classified value that day — an empty
// map (with `is_incomplete=true`) is a real gap day, never a zero-filled
// 100% stack.
export interface AllocationPoint {
  date: string;
  weights: Record<string, string>;
  is_incomplete: boolean;
  excluded_holding_count: number;
}

export interface Allocation {
  // Closed-taxonomy keys that occur anywhere in `points`, already in the
  // backend's fixed display order — color by this order, never by
  // per-date rank.
  asset_classes: string[];
  points: AllocationPoint[];
}

export type MonthlyPartialReason = "range_start" | "tracking_start" | "month_to_date";

export interface MonthlyPerformancePoint {
  month: string; // "YYYY-MM"
  start_date: string;
  end_date: string;
  portfolio_return_pct: string | null;
  benchmark_return_pct: string | null;
  partial_reason: MonthlyPartialReason | null;
  is_approximate: boolean;
  benchmark_unavailable_reason: BenchmarkUnavailableReason | null;
}

export interface MonthlyPerformance {
  method: string;
  benchmark_code: BenchmarkCode;
  points: MonthlyPerformancePoint[];
}

export interface PortfolioPerformanceResponse {
  portfolio: PortfolioPerformanceSeries;
  benchmarks: BenchmarkPerformanceSeries[];
  header: PortfolioPerformanceHeader;
  allocation: Allocation;
  monthly_performance: MonthlyPerformance;
  meta: PortfolioPerformanceMeta;
}

export const DEFAULT_MONTHLY_BENCHMARK: BenchmarkCode = "sp500";

export interface PortfolioPerformanceQuery {
  range: PerformanceRange;
  benchmarks: BenchmarkCode[];
  markets: string[];
  groups: string[];
  brokers: string[];
  accounts: string[];
  twr: boolean;
  baseCurrency: string;
  // Issue #433: single-select, independent of the cumulative `benchmarks`
  // multi-select — drives only `monthly_performance`.
  monthlyBenchmark: BenchmarkCode;
}

// GET /portfolio/performance (issue #360 Phase 2). `baseCurrency` is always
// sent explicitly (seeded from the user's own report-currency preference via
// the summary the server component fetched) so the chart and the header's $
// figures are consistent with what the /portfolio overview shows. Omitting a
// dimension sends no param for it = "no filter" (ALL), matching the backend's
// default; the backend ANDs the dimensions that ARE present.
//
// `benchmarks` is not a dimension filter: an empty list appends no keys,
// FastAPI reads that as None, and the router expands it to zero series
// (portfolio-only). Issue #382: do not treat empty as "the full catalog".
export async function getPortfolioPerformance(
  query: PortfolioPerformanceQuery,
): Promise<PortfolioPerformanceResponse> {
  const params = new URLSearchParams({
    range: query.range,
    twr: String(query.twr),
    monthly_benchmark: query.monthlyBenchmark,
  });
  const dimensions = [
    ["benchmarks", query.benchmarks],
    ["markets", query.markets],
    ["groups", query.groups],
    ["brokers", query.brokers],
    ["accounts", query.accounts],
  ] as const satisfies readonly (readonly [string, readonly string[]])[];
  for (const [name, values] of dimensions) {
    for (const value of values) params.append(name, value);
  }
  params.set("base_currency", query.baseCurrency);
  const res = await fetch(`/api/portfolio/performance?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<PortfolioPerformanceResponse>;
}

export type CheckoutConfig = {
  environment: "sandbox" | "production";
  client_token: string;
  user_id: string;
  email: string;
  packs: { price_id: string; credits: string }[];
};

export async function getCheckoutConfig(): Promise<CheckoutConfig | null> {
  try {
    const response = await fetch("/api/payments/checkout-config", { cache: "no-store" });
    return response.ok ? (response.json() as Promise<CheckoutConfig>) : null;
  } catch {
    return null;
  }
}

export type PurchaseStatus = {
  transaction_id: string;
  credited: boolean;
  credits: string | null;
};

export async function getPurchaseStatus(transactionId: string): Promise<PurchaseStatus | null> {
  try {
    const response = await fetch("/api/payments/purchases/" + transactionId, { cache: "no-store" });
    return response.ok ? ((await response.json()) as PurchaseStatus) : null;
  } catch {
    return null;
  }
}

export interface ReportListItem {
  kind: "report";
  id: string;
  report_date: string;
  report_type: string;
  session_node: string;
  status: string;
  display_state: "available" | "under_review" | "generating";
  generated_at: string | null;
  created_at: string;
}
export interface ReportListPage {
  items: ReportListItem[];
  page: number;
  page_size: number;
  total: number;
  kinds: "report"[];
}
export interface ReportDetail {
  id: string;
  report_date: string;
  report_type: string;
  session_node: string;
  status: string;
  prompt_version: string | null;
  disclaimer_version: string | null;
  report_md: string;
  report_body_html: string;
  generated_at: string | null;
  email_sent_at: string | null;
  created_at: string;
}
export async function listReports(query: Record<string, string>): Promise<ReportListPage> {
  const res = await fetch(`/api/reports?${new URLSearchParams(query).toString()}`, { cache: "no-store" });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<ReportListPage>;
}

export interface ApiToken {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  status: "active" | "expired" | "expired_unused";
}

export interface CreatedApiToken {
  token: string;
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  expires_at: string | null;
}

export async function listApiTokens(): Promise<ApiToken[]> {
  const response = await fetch("/api/me/api-tokens", { cache: "no-store" });
  if (!response.ok) await throwOnHttpError(response);
  return response.json() as Promise<ApiToken[]>;
}

export async function createApiToken(name: string, expires_on: string | null): Promise<CreatedApiToken> {
  const response = await fetch("/api/me/api-tokens", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, expires_on }) });
  if (!response.ok) await throwOnHttpError(response);
  return response.json() as Promise<CreatedApiToken>;
}

export async function revokeApiToken(id: string): Promise<void> {
  const response = await fetch(`/api/me/api-tokens/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!response.ok) await throwOnHttpError(response);
}


export async function setJadeCadence(cadence: BriefingPlan): Promise<Subscription> {
  const res = await fetch("/api/me/jade/cadence", {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cadence }),
  });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<Subscription>;
}

// Jade holdings replay; ratios are serialized decimal strings.
export const REPLAY_RANGES = ["1M", "3M", "6M", "YTD", "1Y", "3Y", "5Y"] as const;
export type ReplayRange = (typeof REPLAY_RANGES)[number];
export interface ReplayMetrics {
  cumulative_return: string;
  annualized_return: string;
  annualized_vol: string;
  max_drawdown: string;
  max_drawdown_peak: string;
  max_drawdown_trough: string;
  worst_day: string;
  worst_day_date: string;
  worst_month: string | null;
  worst_month_label: string | null;
}
export interface ReplayHolding {
  holding_id: string;
  name: string;
  weight: string | null;
  method: "own" | "fund_nav" | "head_proxy" | "proxy" | "cash" | "cash_assumed" | "excluded";
  excluded_reason: "unvalued" | "pending" | "data_unavailable" | null;
  own_history_unavailable: boolean;
  proxy_symbol: string | null;
  proxy_name: string | null;
  beta: string | null;
  beta_samples: number | null;
  own_first_date: string | null;
  own_vol: string | null;
  proxy_segment_vol: string | null;
}
export interface JadeReplay {
  range: ReplayRange;
  status: "ok" | "pending" | "no_holdings" | "insufficient";
  base_currency: string;
  benchmark: BenchmarkCode;
  benchmark_symbol: string;
  benchmark_name: string;
  benchmark_status: "ok" | "pending" | "unavailable";
  window_start: string;
  window_end: string;
  first_valid_date: string | null;
  sample_count: number;
  skipped_days: number;
  points: { date: string; portfolio: string | null; benchmark: string | null }[];
  metrics: { portfolio: ReplayMetrics | null; benchmark: ReplayMetrics | null };
  coverage: {
    own_share: string;
    head_proxy_share: string;
    proxy_share: string;
    cash_share: string;
    cash_assumed_share: string;
    approx_share_at_start: string;
    data_quality: boolean;
    pending_share: string | null;
  };
  holdings: ReplayHolding[];
}
export async function getJadeReplay(baseCurrency?: BaseCurrency, benchmark: BenchmarkCode = "sp500", range?: ReplayRange): Promise<JadeReplay> {
  const params = new URLSearchParams({ benchmark });
  if (baseCurrency) params.set("base_currency", baseCurrency);
  if (range) params.set("range", range);
  const res = await fetch(`/api/jade/replay?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<JadeReplay>;
}


export interface TailCell {
  var: string;
  cvar: string;
  var_amount: string | null;
  cvar_amount: string | null;
}
export interface TailLevel {
  level: 95 | 99;
  available: boolean;
  tail_days: number | null;
  tail_windows: number | null;
  daily: TailCell | null;
  monthly: TailCell | null;
  normal_daily: TailCell | null;
  normal_monthly: TailCell | null;
  benchmark_daily: TailCell | null;
  benchmark_monthly: TailCell | null;
  reference_daily: string | null;
  reference_monthly: string | null;
}
export interface HistogramBin { lower: string; upper: string; count: number }
export interface JadeTailRisk {
  status: JadeReplay["status"];
  base_currency: string;
  benchmark: BenchmarkCode;
  benchmark_symbol: string;
  benchmark_name: string;
  benchmark_status: JadeReplay["benchmark_status"];
  window_start: string;
  window_end: string;
  first_valid_date: string | null;
  sample_count: number;
  month_windows: number;
  month_independent: number;
  portfolio_value: string | null;
  levels: TailLevel[];
  histogram: HistogramBin[];
  tolerance_status: "ok" | "no_questionnaire";
  coverage: JadeReplay["coverage"];
  proxy_understates: boolean;
}
export async function getJadeTailRisk(baseCurrency?: BaseCurrency, benchmark: BenchmarkCode = "sp500"): Promise<JadeTailRisk> {
  const params = new URLSearchParams({ benchmark });
  if (baseCurrency) params.set("base_currency", baseCurrency);
  const res = await fetch(`/api/jade/tail-risk?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) await throwOnHttpError(res);
  return res.json() as Promise<JadeTailRisk>;
}
