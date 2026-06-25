import { messages } from "@/lib/messages";
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

const m = messages.holdings;

function num(value: number | null): string {
  return value === null ? "—" : String(value);
}

function str(value: string | null): string {
  return value ?? "—";
}

export function PreviewTable({ rows }: { rows: ParsedRow[] }) {
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
          <TableHead>{m.colIssues}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((r, i) => {
          const inferred = r.issues.length > 0 || r.confidence < 0.7;
          return (
            <TableRow
              key={i}
              className={
                inferred ? "bg-amber-50 dark:bg-amber-950/30" : undefined
              }
            >
              <TableCell className="font-medium">{r.name}</TableCell>
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
                  variant={r.pricing_mode === "auto" ? "secondary" : "outline"}
                >
                  {r.pricing_mode}
                </Badge>
              </TableCell>
              <TableCell>{str(r.broker)}</TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {r.issues.length > 0 ? r.issues.join("; ") : ""}
              </TableCell>
            </TableRow>
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
  if (groups.length === 0) return null;
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">{m.summaryHeading}</h3>
      <p className="text-xs text-muted-foreground">{m.summaryHint}</p>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{m.summaryColBroker}</TableHead>
            <TableHead className="text-right">{m.summaryColCount}</TableHead>
            <TableHead className="text-right">
              {m.summaryColCostBasis}
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
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{m.colRaw}</TableHead>
          <TableHead>{m.colReason}</TableHead>
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
