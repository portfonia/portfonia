"use client";

// Asset-class allocation history — a separate 100%-stacked area chart below
// the cumulative performance chart (issue #433 requirement 1). Uses
// Recharts' `stackOffset="expand"` so each date's classes always fill the
// 0-100% band; a gap date (all-null row, built by allocation-data.ts) draws
// as an empty band rather than a fabricated 100% stack.

import { useLocale } from "@/app/_components/locale-provider";
import { useTranslations } from "next-intl";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
  type TooltipPayloadEntry,
} from "recharts";
import { useMemo } from "react";
import { formatFullDate, formatShortDate, formatSignedPct } from "./performance-format";
import {
  ASSET_CLASS_COLORS,
  DEFAULT_ASSET_CLASS_COLOR,
  type AllocationPointMeta,
  type AllocationRow,
} from "./allocation-data";

function rowValue(row: AllocationRow, key: string): number | null {
  const value = row[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function AllocationTooltip({
  active,
  payload,
  assetClasses,
  assetClassNames,
  pointMeta,
}: {
  active?: boolean;
  payload?: readonly TooltipPayloadEntry[];
  assetClasses: readonly string[];
  assetClassNames: Record<string, string>;
  pointMeta: Record<string, AllocationPointMeta>;
}) {
  const { locale } = useLocale();
  const t = useTranslations("portfolio.performance");
  const row = payload?.[0]?.payload as AllocationRow | undefined;
  if (!active || !row) return null;
  const meta = pointMeta[row.date];

  const entries = assetClasses
    .map((cls) => {
      const value = rowValue(row, cls);
      if (value === null) return null;
      return (
        <li key={cls} className="flex items-center justify-between gap-4">
          <span className="flex items-center gap-2 truncate">
            <span
              aria-hidden="true"
              className="size-2 shrink-0 rounded-full"
              style={{ backgroundColor: ASSET_CLASS_COLORS[cls] ?? DEFAULT_ASSET_CLASS_COLOR }}
            />
            <span className="truncate">{assetClassNames[cls] ?? cls}</span>
          </span>
          <span className="shrink-0 tabular-nums">{formatSignedPct(value, 1)}</span>
        </li>
      );
    })
    .filter((entry) => entry !== null);

  return (
    <div className="rounded-lg border border-border bg-card/95 px-3 py-2 text-sm shadow-lg backdrop-blur-sm">
      <p className="mb-1 font-medium tabular-nums">{formatFullDate(row.date, locale)}</p>
      {entries.length > 0 ? (
        <ul className="flex flex-col gap-1">{entries}</ul>
      ) : (
        <p className="text-xs text-muted-foreground">{t("allocationGapTooltip")}</p>
      )}
      {meta?.isIncomplete ? (
        <p className="mt-1 text-xs text-muted-foreground">
          {t("allocationIncompleteTooltip", { n: meta.excludedHoldingCount })}
        </p>
      ) : null}
    </div>
  );
}

export function AllocationChart({
  rows,
  assetClasses,
  assetClassNames,
  pointMeta,
}: {
  rows: readonly AllocationRow[];
  assetClasses: readonly string[];
  assetClassNames: Record<string, string>;
  pointMeta: Record<string, AllocationPointMeta>;
}) {
  const { locale } = useLocale();

  const ticks = useMemo(() => {
    if (rows.length <= 6) return rows.map((row) => row.date);
    const step = (rows.length - 1) / 5;
    return Array.from({ length: 6 }, (_, i) => rows[Math.round(i * step)].date);
  }, [rows]);

  if (rows.length === 0) return null;

  const tooltipContent = (props: TooltipContentProps) => (
    <AllocationTooltip
      {...props}
      assetClasses={assetClasses}
      assetClassNames={assetClassNames}
      pointMeta={pointMeta}
    />
  );

  return (
    <div className="h-72 w-full" data-testid="allocation-chart">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart
          data={rows as AllocationRow[]}
          stackOffset="expand"
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
            domain={[0, 1]}
            tickFormatter={(value: number) => `${Math.round(value * 100)}%`}
            tickLine={false}
            axisLine={false}
            tick={{ fill: "var(--muted-foreground)", fontSize: 12 }}
            width={40}
          />
          <Tooltip content={tooltipContent} />
          {assetClasses.map((cls) => (
            <Area
              key={cls}
              type="monotone"
              dataKey={cls}
              stackId="allocation"
              stroke={ASSET_CLASS_COLORS[cls] ?? DEFAULT_ASSET_CLASS_COLOR}
              fill={ASSET_CLASS_COLORS[cls] ?? DEFAULT_ASSET_CLASS_COLOR}
              fillOpacity={0.75}
              isAnimationActive={false}
              connectNulls={false}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
