"use client";

// Portfolio-vs-benchmarks line chart (issue #360 Phase 2). Recharts, same
// library the /portfolio breakdown cards use. The portfolio line is split
// into a solid column and a dashed `portfolioApprox` column by the data
// helper, so an approximate stretch reads as dashes without a second legend
// entry. Pure presentational: data/row building and copy resolution happen
// in the page body / performance-data.ts.

import { useLocale } from "@/app/_components/locale-provider";
import { useTranslations } from "next-intl";
import { formatFullDate, formatShortDate, formatSignedPct, formatTickPct } from "./performance-format";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
  type TooltipPayloadEntry,
} from "recharts";
import { useMemo } from "react";
import type { BenchmarkCode } from "@/lib/api";
import type { BuiltChartData, ChartSeriesRow } from "./performance-data";

export interface ChartSeriesSpec {
  // Chart data column key (portfolio / portfolioApprox / a benchmark code).
  key: string;
  label: string;
  color: string;
  dashed?: boolean;
  isPortfolio?: boolean;
  singletonDot?: boolean;
}

const APPROX_DASH = "5 4";

function rowValue(row: ChartSeriesRow, key: string): number | null {
  const value = row[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function ChartTooltip({
  active,
  payload,
  series,
  pointMeta,
}: {
  active?: boolean;
  payload?: readonly TooltipPayloadEntry[];
  series: readonly ChartSeriesSpec[];
  pointMeta: BuiltChartData["pointMeta"];
}) {
  const { locale } = useLocale();
  const t = useTranslations("portfolio.performance");
  const row = payload?.[0]?.payload as ChartSeriesRow | undefined;
  if (!active || !row) return null;

  const entries = series
    .map((spec) => {
      const value = rowValue(row, spec.key);
      if (value === null) return null;
      const meta =
        spec.isPortfolio || spec.key === "portfolioApprox"
          ? undefined
          : pointMeta[row.date]?.[spec.key as BenchmarkCode];
      return (
        <li key={spec.key} className="flex flex-col gap-0.5">
          <span className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-2 truncate">
              <span
                aria-hidden="true"
                className="size-2 shrink-0 rounded-full"
                style={{ backgroundColor: spec.color }}
              />
              <span className="truncate">{spec.label}</span>
            </span>
            <span className="shrink-0 tabular-nums">{formatSignedPct(value)}</span>
          </span>
          {meta ? (
            <span className="pl-4 text-xs text-muted-foreground">
              {t("tooltipCloseAsOf", { date: formatFullDate(meta.priceAsOf ?? row.date, locale) })}
              {Object.entries(meta.fxAsOf).map(([pair, source]) => (
                <span key={pair} className="block">
                  {t("tooltipFxAsOf", { pair, date: formatFullDate(source, locale) })}
                </span>
              ))}
              {meta.carried ? <span className="block">{t("tooltipCarried")}</span> : null}
            </span>
          ) : null}
        </li>
      );
    })
    .filter((entry) => entry !== null);

  if (entries.length === 0) return null;

  return (
    <div className="rounded-lg border border-border bg-card/95 px-3 py-2 text-sm shadow-lg backdrop-blur-sm">
      <p className="mb-1 font-medium tabular-nums">{formatFullDate(row.date, locale)}</p>
      <ul className="flex flex-col gap-1">{entries}</ul>
    </div>
  );
}

export function PerformanceChart({
  rows,
  series,
  pointMeta,
  anchorDate,
}: {
  rows: readonly ChartSeriesRow[];
  series: readonly ChartSeriesSpec[];
  pointMeta: BuiltChartData["pointMeta"];
  anchorDate: string | null;
}) {
  const { locale } = useLocale();
  const yDomain = useMemo(() => {
    let min = 0;
    let max = 0;
    for (const row of rows) {
      for (const spec of series) {
        const value = rowValue(row, spec.key);
        if (value === null) continue;
        if (value < min) min = value;
        if (value > max) max = value;
      }
    }
    const span = max - min;
    const pad = span === 0 ? Math.max(Math.abs(max) * 0.05, 0.001) : span * 0.08;
    return [min - pad, max + pad];
  }, [rows, series]);

  const ticks = useMemo(() => {
    if (rows.length <= 6) return rows.map((row) => row.date);
    const step = (rows.length - 1) / 5;
    return Array.from({ length: 6 }, (_, i) => rows[Math.round(i * step)].date);
  }, [rows]);

  if (rows.length === 0) return null;

  const tooltipContent = (props: TooltipContentProps) => (
    <ChartTooltip {...props} series={series} pointMeta={pointMeta} />
  );

  return (
    <div
      aria-hidden="true"
      className="h-80 w-full"
      data-testid="performance-chart"
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart
          data={rows as ChartSeriesRow[]}
          margin={{ top: 8, right: 16, bottom: 0, left: 0 }}
        >
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="date"
            ticks={ticks}
            tickFormatter={(date: string) => formatShortDate(date, locale)}
            tickLine={false}
            axisLine={false}
            tick={{ fill: "var(--muted-foreground)", fontSize: 12 }}
            minTickGap={24}
          />
          <YAxis
            tickFormatter={formatTickPct}
            domain={yDomain}
            tickLine={false}
            axisLine={false}
            tick={{ fill: "var(--muted-foreground)", fontSize: 12 }}
            width={48}
          />
          {anchorDate ? (
            <ReferenceLine
              x={anchorDate}
              stroke="var(--muted-foreground)"
              strokeDasharray="3 3"
            />
          ) : null}
          <Tooltip content={tooltipContent} />
          {series.map((spec) => (
            <Line
              key={spec.key}
              type="monotone"
              dataKey={spec.key}
              stroke={spec.color}
              strokeWidth={spec.isPortfolio ? 2.5 : 1.5}
              strokeDasharray={spec.dashed ? APPROX_DASH : undefined}
              dot={
                spec.singletonDot
                  ? { r: 4, strokeWidth: 0, fill: spec.color }
                  : false
              }
              activeDot={{ r: 4 }}
              isAnimationActive={false}
              connectNulls={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
