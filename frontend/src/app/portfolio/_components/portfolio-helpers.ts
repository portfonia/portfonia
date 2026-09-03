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

// Issue #330: the currency card's three display modes.
export type CurrencyDisplayMode = "native" | "normalized" | "percentage";
export const CURRENCY_DISPLAY_MODES: CurrencyDisplayMode[] = [
  "native",
  "normalized",
  "percentage",
];

// 本币 mode: group the priced holdings list by their own (native) currency,
// summing native `market_value` — no base-currency conversion. Matches
// by_currency's exclusion of holdings with no valuation (null market_value),
// so switching modes never changes which holdings are represented.
export function nativeCurrencyBreakdown(holdings: HoldingValueOut[]): Record<string, string> {
  const totals: Record<string, number> = {};
  for (const holding of holdings) {
    if (holding.market_value === null) continue;
    totals[holding.currency] = (totals[holding.currency] ?? 0) + Number(holding.market_value);
  }
  return Object.fromEntries(
    Object.entries(totals).map(([currency, total]) => [currency, total.toFixed(2)]),
  );
}

// 比例 mode: each normalized (by_currency) bucket as a percentage (0-100) of
// the portfolio total. Computed from by_currency, not re-derived from
// holdings, so it always matches whatever base currency the page is
// currently showing.
export function currencySharePercentages(byCurrency: Record<string, string>): Record<string, string> {
  const total = Object.values(byCurrency).reduce((sum, value) => sum + Number(value), 0);
  if (total <= 0) return {};
  return Object.fromEntries(
    Object.entries(byCurrency).map(([currency, value]) => [
      currency,
      ((Number(value) / total) * 100).toFixed(2),
    ]),
  );
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
