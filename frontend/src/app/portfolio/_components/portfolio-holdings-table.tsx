"use client";

import { useTranslations } from "next-intl";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { HoldingValueOut } from "@/lib/api";
import { formatMoney, formatPercent, pnlColorClass } from "./portfolio-helpers";

function cell(value: string | null): string {
  return value ?? "—";
}

// Priced holdings only (the isNoLivePrice partition happens one level up in
// PortfolioPageBody) — cash/wmf rows appear here with a real market value
// and "—" in every P&L column, per issue #320 decision 4.
export function PortfolioHoldingsTable({
  holdings,
  baseCurrency,
}: {
  holdings: HoldingValueOut[];
  baseCurrency: string;
}) {
  const t = useTranslations("portfolio");
  if (holdings.length === 0) {
    return <p className="text-sm text-muted-foreground">{t("holdingsEmptyState")}</p>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{t("colName")}</TableHead>
          <TableHead>{t("colMarket")}</TableHead>
          <TableHead>{t("colGroup")}</TableHead>
          <TableHead>{t("colCustodian")}</TableHead>
          <TableHead className="text-right">{t("colMarketValue")}</TableHead>
          <TableHead className="text-right">{t("colUnrealizedPnl")}</TableHead>
          <TableHead className="text-right">{t("colUnrealizedPnlPct")}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {holdings.map((h) => (
          <TableRow key={h.holding_id}>
            <TableCell className="font-medium">
              {h.name}
              {h.ticker || h.fund_code ? (
                <span className="ml-1.5 text-xs text-muted-foreground">
                  {cell(h.ticker ?? h.fund_code)}
                </span>
              ) : null}
            </TableCell>
            <TableCell>{h.market}</TableCell>
            <TableCell>{cell(h.portfolio)}</TableCell>
            <TableCell>{cell(h.broker)}</TableCell>
            <TableCell className="text-right tabular-nums">
              {formatMoney(h.market_value_base, baseCurrency)}
            </TableCell>
            <TableCell
              className={`text-right tabular-nums ${pnlColorClass(h.unrealized_pnl_base)}`}
            >
              {formatMoney(h.unrealized_pnl_base, baseCurrency)}
            </TableCell>
            <TableCell
              className={`text-right tabular-nums ${pnlColorClass(h.unrealized_pnl_pct)}`}
            >
              {formatPercent(h.unrealized_pnl_pct)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
