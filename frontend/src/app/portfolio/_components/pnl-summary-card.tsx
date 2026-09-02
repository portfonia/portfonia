"use client";

import { useTranslations } from "next-intl";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { PortfolioSummary } from "@/lib/api";
import { formatMoney, formatPercent, pnlColorClass } from "./portfolio-helpers";

// Issue #320 decision 2: these totals exclude cash/wmf (no cost-basis
// concept applies) — the description line states that scope so "total
// unrealized return %" isn't misread as covering all assets.
export function PnlSummaryCard({ summary }: { summary: PortfolioSummary }) {
  const t = useTranslations("portfolio");
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
            {formatMoney(summary.total_cost_basis_base, summary.base_currency)}
          </span>
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-xs text-foreground/60">{t("pnlTotalUnrealized")}</span>
          <span className={`tabular-nums ${pnlClass}`}>
            {formatMoney(summary.total_unrealized_pnl_base, summary.base_currency)}
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
