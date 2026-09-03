"use client";

import type { ReactNode } from "react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatMoney } from "./portfolio-helpers";

// The existing shadcn chart tokens (globals.css), already theme-aware —
// reusing them means dark/light mode "just works" with no chart-specific
// palette to maintain.
const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
];

interface Slice {
  key: string; // raw backend dict key — guaranteed unique, used for React/Cell keys
  name: string; // display label — may collide with another slice's label, harmlessly
  value: number;
  formatted: string; // precomputed amount text — read back from the recharts Tooltip payload
}

export function BreakdownChart({
  title,
  description,
  data,
  currency,
  emptyLabel,
  labelFor,
  formatValue,
  showShareOfTotal = true,
  headerControl,
}: {
  title: string;
  description?: string;
  data: Record<string, string>;
  currency: string;
  emptyLabel: string;
  // Grok review round 2 (PR #322): round 1 pre-translated the fallback key
  // (e.g. "Ungrouped" -> "未分组") by rewriting the Record's own keys before
  // this component saw them — if a user's real group/broker name happened
  // to equal that translated string, Object.fromEntries silently collapsed
  // the two into one, dropping a slice. Translating only at display time
  // (this prop), while grouping on the untouched raw key, can't collide.
  labelFor?: (key: string) => string;
  // Issue #330: the currency card's 本币/比例 modes aren't a single-currency
  // money amount (本币 has a different currency per bucket; 比例 isn't money
  // at all) — overriding the whole formatted string per (key, value) covers
  // both without this component needing to know about display modes.
  // Defaults to money in the page's `currency`, unchanged for every other
  // caller.
  formatValue?: (key: string, value: number) => string;
  // 比例 mode's value is already a share of the total, so the legend's own
  // "(NN.N%)" annotation would just repeat the main figure — suppress it
  // there. Every other caller keeps the annotation (default true).
  showShareOfTotal?: boolean;
  // Issue #330: the currency card's mode switcher is local to that one card
  // (design contract), not a page-level control — rendered in the header
  // next to the title rather than lifted out as a separate component prop
  // every other caller would have to pass null for.
  headerControl?: ReactNode;
}) {
  const format = formatValue ?? ((_key: string, value: number) => formatMoney(String(value), currency));
  const total = Object.values(data).reduce((sum, v) => sum + Number(v), 0);
  const slices: Slice[] = Object.entries(data)
    .map(([key, value]) => ({
      key,
      name: labelFor ? labelFor(key) : key,
      value: Number(value),
      formatted: format(key, Number(value)),
    }))
    .filter((slice) => slice.value > 0)
    .sort((a, b) => b.value - a.value);

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-3">
        <div>
          <CardTitle>{title}</CardTitle>
          {description ? <CardDescription>{description}</CardDescription> : null}
        </div>
        {headerControl}
      </CardHeader>
      <CardContent className="flex flex-col gap-4 px-4">
        {slices.length === 0 ? (
          <p className="text-sm text-muted-foreground">{emptyLabel}</p>
        ) : (
          <>
            <div className="h-56 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={slices}
                    dataKey="value"
                    nameKey="name"
                    innerRadius={48}
                    outerRadius={84}
                    paddingAngle={2}
                  >
                    {slices.map((slice, index) => (
                      <Cell key={slice.key} fill={CHART_COLORS[index % CHART_COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip
                    formatter={(_value, _name, item) => (item.payload as Slice).formatted}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <ul className="flex flex-col gap-1.5 text-sm">
              {slices.map((slice, index) => (
                <li key={slice.key} className="flex items-center justify-between gap-3">
                  <span className="flex items-center gap-2 truncate">
                    <span
                      aria-hidden="true"
                      className="size-2.5 shrink-0 rounded-full"
                      style={{
                        backgroundColor: CHART_COLORS[index % CHART_COLORS.length],
                      }}
                    />
                    <span className="truncate">{slice.name}</span>
                  </span>
                  <span className="shrink-0 tabular-nums text-foreground/80">
                    {slice.formatted}
                    {showShareOfTotal && total > 0
                      ? ` (${((slice.value / total) * 100).toFixed(1)}%)`
                      : null}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
      </CardContent>
    </Card>
  );
}
