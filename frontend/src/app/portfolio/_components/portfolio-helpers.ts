import type { HoldingValueOut } from "@/lib/api";

// Issue #320 decision 3: the determining condition is pricing_mode=="auto"
// and not capture_supported — NOT the broader market_value_base==null,
// which would also catch a normal holding with a transient missing price or
// FX rate and misclassify a temporary data gap as a permanently-unsupported
// market.
export function isNoLivePrice(holding: HoldingValueOut): boolean {
  return holding.pricing_mode === "auto" && !holding.capture_supported;
}

export function partitionHoldings(holdings: HoldingValueOut[]): {
  priced: HoldingValueOut[];
  noLivePrice: HoldingValueOut[];
} {
  const priced: HoldingValueOut[] = [];
  const noLivePrice: HoldingValueOut[] = [];
  for (const holding of holdings) {
    (isNoLivePrice(holding) ? noLivePrice : priced).push(holding);
  }
  return { priced, noLivePrice };
}

// Deliberately not Intl.NumberFormat's currency style: CNH is a real,
// actively-used currency in this app (offshore yuan) but not a valid ISO
// 4217 code, and several supported currencies share the "$" symbol (USD,
// CAD, AUD, SGD, HKD) — ambiguous on a page whose entire point is showing
// value across multiple currencies at once. A plain grouped number plus the
// currency code is unambiguous everywhere.
export function formatMoney(value: string | null, currency: string): string {
  if (value === null) return "—";
  const formatted = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(value));
  return `${formatted} ${currency}`;
}

// value is a ratio (0.2 = 20%), matching the backend's unrealized_pnl_pct /
// total_unrealized_pnl_pct convention.
export function formatPercent(value: string | null): string {
  if (value === null) return "—";
  const pct = Number(value) * 100;
  const sign = pct >= 0 ? "+" : "";
  return `${sign}${pct.toFixed(2)}%`;
}

// Must match portfolio_calculator.py's `h.portfolio or "Ungrouped"` /
// `h.broker or "Other"` / `h.account or "Other"` (report_sections.py's
// existing broker fallback literal) exactly — these are the raw dict keys
// the backend sends in by_group/by_broker/by_account. by_broker and
// by_account share the same "Other" fallback literal (issue #330), so one
// constant covers both.
export const GROUP_UNGROUPED_KEY = "Ungrouped";
export const ACCOUNT_OTHER_KEY = "Other";

// Group the priced holdings list by their own (native) currency, summing
// native `market_value` — no base-currency conversion. Excludes a holding
// whose market_value_base is null (e.g. a stale FX pair) even when its
// native market_value is present — the same gate portfolio_calculator.py
// applies before adding to by_currency, so this native total and
// by_currency's base total always represent the exact same set of
// holdings (issue #330 review round 1; still required post-#350: the
// by-currency card row now shows both figures side by side, so a mismatch
// there would be visible on every row, not just a hidden mode-switch bug).
export function nativeCurrencyBreakdown(holdings: HoldingValueOut[]): Record<string, string> {
  const totals: Record<string, number> = {};
  for (const holding of holdings) {
    if (holding.market_value === null || holding.market_value_base === null) continue;
    totals[holding.currency] = (totals[holding.currency] ?? 0) + Number(holding.market_value);
  }
  return Object.fromEntries(
    Object.entries(totals).map(([currency, total]) => [currency, total.toFixed(2)]),
  );
}

// Issue #350 item 2: the by-currency card's unified row format — confirmed
// with the product owner rather than derived from the ambiguous original
// wording: "{native amount} {native currency code} / {base-currency
// amount} {base currency code} ({share of total}%)", e.g.
// "2,457,658.27 CNY / 366,743.50 USD (53.5%)". `nativeAmount` is the raw
// (unformatted) native total for this currency bucket — "0.00" when the
// bucket has no native total, which can't actually happen here since both
// figures are keyed off the same by_currency currency code, but keeps this
// function total rather than assuming its caller's map lookup always hits.
export function formatCurrencyBreakdownRow(
  nativeAmount: string,
  nativeCurrency: string,
  baseAmount: number,
  baseCurrency: string,
  sharePct: number,
): string {
  return `${formatMoney(nativeAmount, nativeCurrency)} / ${formatMoney(String(baseAmount), baseCurrency)} (${sharePct.toFixed(1)}%)`;
}

// Same fallback semantics as the backend's `h.field or literal`, for the
// holdings table row (so a row's Group/Custodian cell reads the same label
// as the chart slice it rolls up into, instead of a plain "—").
export function fallbackOrValue(value: string | null, translatedFallback: string): string {
  return value || translatedFallback;
}

// Shared by the P&L summary card and the holdings table row, so a gain/loss
// reads the same color everywhere on the page.
export function pnlColorClass(value: string | null): string {
  if (value === null) return "text-foreground/60";
  const n = Number(value);
  if (n > 0) return "text-emerald-600 dark:text-emerald-400";
  if (n < 0) return "text-destructive";
  return "text-foreground";
}
