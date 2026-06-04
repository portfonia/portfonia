import { messages } from "@/lib/messages";
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

const m = messages.holdings;

function cell(value: string | null): string {
  return value ?? "—";
}

export function HoldingsTable({ holdings }: { holdings: HoldingOut[] }) {
  if (holdings.length === 0) {
    return <p className="text-sm text-muted-foreground">{m.emptyState}</p>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{m.colName}</TableHead>
          <TableHead>{m.colTicker}</TableHead>
          <TableHead>{m.colCurrency}</TableHead>
          <TableHead className="text-right">{m.colShares}</TableHead>
          <TableHead className="text-right">{m.colAvgCost}</TableHead>
          <TableHead className="text-right">{m.colCurrentValue}</TableHead>
          <TableHead>{m.colPricingMode}</TableHead>
          <TableHead>{m.colBroker}</TableHead>
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
