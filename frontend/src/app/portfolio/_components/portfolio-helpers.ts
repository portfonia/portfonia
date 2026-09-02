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

// Shared by the P&L summary card and the holdings table row, so a gain/loss
// reads the same color everywhere on the page.
export function pnlColorClass(value: string | null): string {
  if (value === null) return "text-foreground/60";
  const n = Number(value);
  if (n > 0) return "text-emerald-600 dark:text-emerald-400";
  if (n < 0) return "text-destructive";
  return "text-foreground";
}
