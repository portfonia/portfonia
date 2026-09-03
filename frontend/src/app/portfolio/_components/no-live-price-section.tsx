"use client";

import { useTranslations } from "next-intl";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { HoldingValueOut } from "@/lib/api";

function cell(value: string | null): string {
  return value ?? "—";
}

// Issue #320 requirement 5: holdings the capture layer can't price at all
// (market=Other, capture_supported=false). Shown so the user's data isn't
// silently dropped, but deliberately excluded from every chart/total — only
// user-entered fields render, no market value, no P&L column. `notes` was
// missing from the frozen HoldingValueOut contract table despite decision 3
// / issue comment 2 both listing it — added to the schema in round 2
// (Grok review, PR #322) rather than leaving the mismatch between spec and
// contract unresolved.
export function NoLivePriceSection({ holdings }: { holdings: HoldingValueOut[] }) {
  const t = useTranslations("portfolio");
  if (holdings.length === 0) return null;

  return (
    // Grok review round 1 (PR #322): variant="urgent" is documented
    // (components/ui/card.tsx, docs/mechanisms/frontend-chrome.md) as the
    // issue #269 "complete this soon" incomplete-setup language — wrong
    // speech act for an informational exclusion notice. Default card.
    <Card>
      <CardHeader>
        <CardTitle>{t("noLivePriceHeading")}</CardTitle>
        <CardDescription>{t("noLivePriceBody")}</CardDescription>
      </CardHeader>
      <CardContent className="px-4">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("colName")}</TableHead>
              <TableHead>{t("colTicker")}</TableHead>
              <TableHead>{t("colCurrency")}</TableHead>
              <TableHead className="text-right">{t("colShares")}</TableHead>
              <TableHead className="text-right">{t("colAvgCost")}</TableHead>
              <TableHead>{t("colCustodian")}</TableHead>
              <TableHead>{t("colNotes")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {holdings.map((h) => (
              <TableRow key={h.holding_id}>
                <TableCell className="font-medium">{h.name}</TableCell>
                <TableCell>{cell(h.ticker ?? h.fund_code)}</TableCell>
                <TableCell>{h.currency}</TableCell>
                <TableCell className="text-right tabular-nums">{cell(h.shares)}</TableCell>
                <TableCell className="text-right tabular-nums">{cell(h.avg_cost)}</TableCell>
                <TableCell>{cell(h.broker)}</TableCell>
                <TableCell className="max-w-48 truncate" title={h.notes ?? undefined}>
                  {cell(h.notes)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
