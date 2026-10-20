"use client";

import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis, type TooltipContentProps } from "recharts";
import type { Example } from "../_lib/example";
import { PORTFOLIO_COLOR, BENCHMARK_COLORS } from "@/app/portfolio/performance/_components/performance-colors";

const UNAVAILABLE = "\u2014";

function seriesRow(payload: TooltipContentProps["payload"] | undefined): Record<string, unknown> | null {
  if (!payload) return null;
  for (const item of payload) {
    if (item.dataKey !== "portfolio" && item.dataKey !== "spy") continue;
    const row: unknown = item.payload;
    if (typeof row === "object" && row !== null) return row as Record<string, unknown>;
  }
  return null;
}

function entryText(row: Record<string, unknown>, key: string, format: (value: number) => string): string {
  const value = row[key];
  return typeof value === "number" && Number.isFinite(value) ? format(value) : UNAVAILABLE;
}

function ReplayTooltip({ active, label, payload, portfolioName, formatPercent, formatTick }: {
  active?: boolean;
  label?: unknown;
  payload?: TooltipContentProps["payload"];
  portfolioName: string;
  formatPercent: (value: number) => string;
  formatTick: (value: number) => string;
}) {
  const row = seriesRow(payload);
  const parsed = typeof label === "number" || typeof label === "string" ? Number(label) : Number.NaN;
  const stamp = Number.isFinite(parsed) ? parsed : typeof row?.time === "number" ? row.time : Number.NaN;
  if (!active || !row || !Number.isFinite(stamp)) return null;
  const series = [
    { key: "portfolio", name: portfolioName, color: PORTFOLIO_COLOR },
    { key: "spy", name: "SPY", color: BENCHMARK_COLORS.sp500 },
  ] as const;
  return <div className="public-curve-tooltip max-w-full break-words rounded-lg border px-3 py-2 text-sm shadow-lg" style={{ background: "var(--card)", color: "var(--foreground)", borderColor: "var(--border)" }}>
    <p className="mb-1 font-medium tabular-nums">{formatTick(stamp)}</p>
    <ul className="flex flex-col gap-1">
      {series.map((item) => <li key={item.key} className="flex items-center justify-between gap-4">
        <span className="flex min-w-0 items-center gap-2">
          <span aria-hidden="true" className="size-2 shrink-0 rounded-full" style={{ backgroundColor: item.color }} />
          <span className="break-words">{item.name}</span>
        </span>
        <span className="shrink-0 tabular-nums">{entryText(row, item.key, formatPercent)}</span>
      </li>)}
    </ul>
  </div>;
}

export function ReplayChart({ points, peakDate, troughDate, portfolioName, formatPercent, formatTick }: {
  points: Example["points"];
  peakDate: string;
  troughDate: string;
  portfolioName: string;
  formatPercent: (v: number) => string;
  formatTick: (v: number) => string;
}) {
  return <ResponsiveContainer width="100%" height="100%">
          <LineChart data={points} margin={{
            top: 8,
            right: 8,
            bottom: 0,
            left: 0
          }}>
            <CartesianGrid stroke="var(--border)" vertical={false} />
            <XAxis type="number" dataKey="time" domain={['dataMin', 'dataMax']} tickFormatter={formatTick} minTickGap={36} tick={{
              fontSize: 10,
              fill: 'var(--muted-foreground)'
            }} />
            <YAxis width={64} tickFormatter={formatPercent} tick={{
              fontSize: 10,
              fill: 'var(--muted-foreground)'
            }} />
            <Tooltip isAnimationActive={false} cursor={{ stroke: "var(--foreground)" }} wrapperStyle={{ maxWidth: "100%" }} content={(props) => <ReplayTooltip active={props.active} label={props.label} payload={props.payload} portfolioName={portfolioName} formatPercent={formatPercent} formatTick={formatTick} />} />
            <Line name={portfolioName} dataKey="portfolio" stroke={PORTFOLIO_COLOR} dot={false} isAnimationActive={false} />
            <Line name="SPY" dataKey="spy" stroke={BENCHMARK_COLORS.sp500} dot={false} isAnimationActive={false} />
            <ReferenceLine x={Date.parse(peakDate)} stroke="var(--muted-foreground)" strokeDasharray="3 3" />
            <ReferenceLine x={Date.parse(troughDate)} stroke="var(--muted-foreground)" strokeDasharray="3 3" />
            <Legend wrapperStyle={{ fontSize: 10 }} />
          </LineChart>
        </ResponsiveContainer>;
}
