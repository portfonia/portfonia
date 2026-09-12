"use client";

// Monthly portfolio-vs-benchmark bar chart (issue #433 requirements 4/5/8).
// Portfolio is always the approximate EOD TWR method regardless of the
// cumulative chart's TWR toggle; the benchmark is the one independently
// single-selected index. A visible zero baseline supports both positive and
// negative bars.

import { useLocale } from "@/app/_components/locale-provider";
import { useTranslations } from "next-intl";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
  type TooltipPayloadEntry,
} from "recharts";
import { useMemo } from "react";
import { formatFullDate, formatSignedPct, formatTickPct } from "./performance-format";
import { MONTHLY_CHART_CURSOR, adaptiveMonthTicks, type MonthlyBarRow } from "./monthly-data";

function monthLabel(month: string, locale: string): string {
  const [year, m] = month.split("-").map(Number);
  return new Intl.DateTimeFormat(locale, { year: "numeric", month: "short" }).format(
    new Date(year, m - 1, 1),
  );
}

function MonthlyTooltip({
  active,
  payload,
  benchmarkName,
}: {
  active?: boolean;
  payload?: readonly TooltipPayloadEntry[];
  benchmarkName: string;
}) {
  const { locale } = useLocale();
  const t = useTranslations("portfolio.performance");
  const row = payload?.[0]?.payload as MonthlyBarRow | undefined;
  if (!active || !row) return null;

  return (
    <div className="rounded-lg border border-border bg-card/95 px-3 py-2 text-sm shadow-lg backdrop-blur-sm">
      <p className="mb-1 font-medium tabular-nums">
        {formatFullDate(row.startDate, locale)} – {formatFullDate(row.endDate, locale)}
      </p>
      <ul className="flex flex-col gap-1">
        <li className="flex items-center justify-between gap-4">
          <span>{t("monthlyPortfolioLabel")}</span>
          <span className="tabular-nums">{formatSignedPct(row.portfolio)}</span>
        </li>
        <li className="flex items-center justify-between gap-4">
          <span>{benchmarkName}</span>
          <span className="tabular-nums">
            {row.benchmark !== null
              ? formatSignedPct(row.benchmark)
              : t(`benchmarkUnavailable.${row.benchmarkUnavailableReason ?? "missing_price"}`)}
          </span>
        </li>
      </ul>
      {row.partialReason ? (
        <p className="mt-1 text-xs text-muted-foreground">
          {t(`monthlyPartialReason.${row.partialReason}`)}
        </p>
      ) : null}
      {row.isApproximate ? (
        <p className="mt-1 text-xs text-muted-foreground">{t("monthlyApproxNote")}</p>
      ) : null}
    </div>
  );
}

export function MonthlyPerformanceChart({
  rows,
  benchmarkName,
  benchmarkColor,
}: {
  rows: readonly MonthlyBarRow[];
  benchmarkName: string;
  benchmarkColor: string;
}) {
  const { locale } = useLocale();
  const t = useTranslations("portfolio.performance");

  const ticks = useMemo(() => adaptiveMonthTicks(rows.map((r) => r.month)), [rows]);

  if (rows.length === 0) return null;

  const tooltipContent = (props: TooltipContentProps) => (
    <MonthlyTooltip {...props} benchmarkName={benchmarkName} />
  );

  return (
    <div className="h-72 w-full" data-testid="monthly-performance-chart">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows as MonthlyBarRow[]} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="month"
            ticks={ticks}
            tickFormatter={(month: string) => monthLabel(month, locale)}
            tickLine={false}
            axisLine={false}
            tick={{ fill: "var(--muted-foreground)", fontSize: 12 }}
            minTickGap={16}
          />
          <YAxis
            tickFormatter={formatTickPct}
            tickLine={false}
            axisLine={false}
            tick={{ fill: "var(--muted-foreground)", fontSize: 12 }}
            width={48}
          />
          <ReferenceLine y={0} stroke="var(--muted-foreground)" />
          <Tooltip content={tooltipContent} cursor={MONTHLY_CHART_CURSOR} />
          <Bar
            dataKey="portfolio"
            name={t("monthlyPortfolioLabel")}
            fill="var(--chart-1)"
            isAnimationActive={false}
          />
          <Bar
            dataKey="benchmark"
            name={benchmarkName}
            fill={benchmarkColor}
            isAnimationActive={false}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
