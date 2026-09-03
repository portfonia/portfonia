"use client";

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
}

export function BreakdownChart({
  title,
  description,
  data,
  currency,
  emptyLabel,
  labelFor,
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
}) {
  const total = Object.values(data).reduce((sum, v) => sum + Number(v), 0);
  const slices: Slice[] = Object.entries(data)
    .map(([key, value]) => ({ key, name: labelFor ? labelFor(key) : key, value: Number(value) }))
    .filter((slice) => slice.value > 0)
    .sort((a, b) => b.value - a.value);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {description ? <CardDescription>{description}</CardDescription> : null}
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
                  <Tooltip formatter={(value) => formatMoney(String(value ?? "0"), currency)} />
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
                    {formatMoney(String(slice.value), currency)}
                    {total > 0 ? ` (${((slice.value / total) * 100).toFixed(1)}%)` : null}
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
