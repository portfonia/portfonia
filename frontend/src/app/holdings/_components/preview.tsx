import { Fragment } from "react";
import { useTranslations } from "next-intl";

import type { ParsedRow, IssueRow, BrokerGroup } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

function num(value: number | null): string {
  return value === null ? "—" : String(value);
}

function str(value: string | null): string {
  return value ?? "—";
}

export function PreviewTable({ rows }: { rows: ParsedRow[] }) {
  const t = useTranslations("holdings");
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{t("colName")}</TableHead>
          <TableHead>{t("colTicker")}</TableHead>
          <TableHead>{t("colCurrency")}</TableHead>
          <TableHead className="text-right">{t("colShares")}</TableHead>
          <TableHead className="text-right">{t("colAvgCost")}</TableHead>
          <TableHead className="text-right">{t("colCurrentValue")}</TableHead>
          <TableHead>{t("colPricingMode")}</TableHead>
          <TableHead>{t("colBroker")}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((r, i) => {
          const inferred = r.issues.length > 0 || r.confidence < 0.7;
          return (
            <Fragment key={i}>
              <TableRow
                className={
                  inferred ? "bg-amber-50 dark:bg-amber-950/30" : undefined
                }
              >
                <TableCell className="font-medium">
                  <span>{r.name}</span>
                  {r.capture_supported === false && (
                    <Badge variant="outline" className="ml-2">
                      {t("unsupportedCaptureBadge")}
                    </Badge>
                  )}
                </TableCell>
                <TableCell>{str(r.ticker ?? r.fund_code)}</TableCell>
                <TableCell>{r.currency}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {num(r.shares)}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {num(r.avg_cost)}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {num(r.current_value)}
                </TableCell>
                <TableCell>
                  <Badge
                    variant={
                      r.pricing_mode === "auto" ? "secondary" : "outline"
                    }
                  >
                    {r.pricing_mode}
                  </Badge>
                </TableCell>
                <TableCell>{str(r.broker)}</TableCell>
              </TableRow>
              {r.issues.length > 0 && (
                <TableRow
                  className={
                    inferred ? "bg-amber-50 dark:bg-amber-950/30" : undefined
                  }
                >
                  <TableCell
                    colSpan={8}
                    className="pt-0 text-xs text-amber-700 dark:text-amber-400"
                  >
                    <p className="font-medium">{t("rowNotesLabel")}</p>
                    <ul className="ml-4 list-disc">
                      {r.issues.map((issue, j) => (
                        <li key={j}>{issue}</li>
                      ))}
                    </ul>
                  </TableCell>
                </TableRow>
              )}
            </Fragment>
          );
        })}
      </TableBody>
    </Table>
  );
}

function fmtCostBasis(value: number, currency: string): string {
  return `${value.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })} ${currency}`;
}

export function BrokerSummary({ groups }: { groups: BrokerGroup[] }) {
  const t = useTranslations("holdings");
  if (groups.length === 0) return null;
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">{t("summaryHeading")}</h3>
      <p className="text-xs text-muted-foreground">{t("summaryHint")}</p>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t("summaryColBroker")}</TableHead>
            <TableHead className="text-right">{t("summaryColCount")}</TableHead>
            <TableHead className="text-right">
              {t("summaryColCostBasis")}
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {groups.map((g, i) => (
            <TableRow key={i}>
              <TableCell className="font-medium">{g.broker}</TableCell>
              <TableCell className="text-right tabular-nums">
                {g.holding_count}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {g.subtotals.length === 0
                  ? "—"
                  : g.subtotals.map((s, j) => (
                      <div key={j}>
                        {fmtCostBasis(s.cost_basis, s.currency)}
                      </div>
                    ))}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export function IssueList({ rows }: { rows: IssueRow[] }) {
  const t = useTranslations("holdings");
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{t("colRaw")}</TableHead>
          <TableHead>{t("colReason")}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((r, i) => (
          <TableRow key={i} className="bg-destructive/5">
            <TableCell className="font-mono text-xs">{r.raw}</TableCell>
            <TableCell className="text-sm text-destructive">
              {r.reason}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
