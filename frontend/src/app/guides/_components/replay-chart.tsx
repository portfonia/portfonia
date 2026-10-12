"use client";

import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, XAxis, YAxis } from "recharts";
import type { Example } from "../_lib/example";
import { PORTFOLIO_COLOR, BENCHMARK_COLORS } from "@/app/portfolio/performance/_components/performance-colors";

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
            <YAxis width={48} tickFormatter={formatPercent} tick={{
              fontSize: 10,
              fill: 'var(--muted-foreground)'
            }} />
            <Line name={portfolioName} dataKey="portfolio" stroke={PORTFOLIO_COLOR} dot={false} isAnimationActive={false} />
            <Line name="SPY" dataKey="spy" stroke={BENCHMARK_COLORS.sp500} dot={false} isAnimationActive={false} />
            <ReferenceLine x={Date.parse(peakDate)} stroke="var(--muted-foreground)" strokeDasharray="3 3" />
            <ReferenceLine x={Date.parse(troughDate)} stroke="var(--muted-foreground)" strokeDasharray="3 3" />
            <Legend wrapperStyle={{ fontSize: 10 }} />
          </LineChart>
        </ResponsiveContainer>;
}
