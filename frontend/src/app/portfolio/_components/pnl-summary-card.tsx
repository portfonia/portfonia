"use client";

import { useTranslations } from "next-intl";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { PortfolioSummary } from "@/lib/api";
import { formatMoney, formatPercent, pnlColorClass } from "./portfolio-helpers";

// Issue #320 decision 2: cash/wmf never get a per-holding cost-basis
// concept. Issue #350 item 5 changed the AGGREGATE though: cash/wmf's
// value now joins total_cost_basis_base (diluting total_unrealized_pnl_pct's
// denominator) while still contributing 0 to the numerator — the
// description line states that so "total unrealized return %" isn't
// misread as excluding cash/wmf, which it no longer does.
export function PnlSummaryCard({ summary }: { summary: PortfolioSummary }) {
  const t = useTranslations("portfolio");
  // Grok review round 3 (PR #322): total_cost_basis_base/total_unrealized_pnl_base
  // are non-optional Decimals on the wire — a cash/wmf-only book (no holding
  // contributed a cost basis) sends "0", not null, so formatMoney rendered
  // "0.00" next to the percent's correctly-null "—". total_unrealized_pnl_pct
  // is the one field the backend only sets when total_cost_basis_base > 0
  // (see compute_portfolio), so it's the reliable "any cost basis at all?"
  // signal — gate all three fields on it rather than trusting the sums.
  const hasCostBasis = summary.total_unrealized_pnl_pct !== null;
  const pnlClass = pnlColorClass(summary.total_unrealized_pnl_base);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("pnlHeading")}</CardTitle>
        <CardDescription>{t("pnlScopeNote")}</CardDescription>
      </CardHeader>
      <CardContent className="grid grid-cols-1 gap-3 px-4 sm:grid-cols-3">
        <div className="flex flex-col gap-1">
          <span className="text-xs text-foreground/60">{t("pnlTotalCostBasis")}</span>
          <span className="tabular-nums">
            {hasCostBasis ? formatMoney(summary.total_cost_basis_base, summary.base_currency) : "—"}
          </span>
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-xs text-foreground/60">{t("pnlTotalUnrealized")}</span>
          <span className={`tabular-nums ${pnlClass}`}>
            {hasCostBasis
              ? formatMoney(summary.total_unrealized_pnl_base, summary.base_currency)
              : "—"}
          </span>
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-xs text-foreground/60">{t("pnlTotalUnrealizedPct")}</span>
          <span className={`tabular-nums ${pnlClass}`}>
            {formatPercent(summary.total_unrealized_pnl_pct)}
          </span>
        </div>
      </CardContent>
    </Card>
  );
}
