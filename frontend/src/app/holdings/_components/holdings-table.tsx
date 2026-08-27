import { useTranslations } from "next-intl";

import type { HoldingOut } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

function cell(value: string | null): string {
  return value ?? "—";
}

export function HoldingsTable({ holdings }: { holdings: HoldingOut[] }) {
  const t = useTranslations("holdings");
  if (holdings.length === 0) {
    return <p className="text-sm text-muted-foreground">{t("emptyState")}</p>;
  }
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
        {holdings.map((h) => (
          <TableRow key={h.id}>
            <TableCell className="font-medium">{h.name}</TableCell>
            <TableCell>{cell(h.ticker ?? h.fund_code)}</TableCell>
            <TableCell>{h.currency}</TableCell>
            <TableCell className="text-right tabular-nums">
              {cell(h.shares)}
            </TableCell>
            <TableCell className="text-right tabular-nums">
              {cell(h.avg_cost)}
            </TableCell>
            <TableCell className="text-right tabular-nums">
              {cell(h.current_value)}
            </TableCell>
            <TableCell>
              <Badge variant={h.pricing_mode === "auto" ? "secondary" : "outline"}>
                {h.pricing_mode}
              </Badge>
            </TableCell>
            <TableCell>{cell(h.broker)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
