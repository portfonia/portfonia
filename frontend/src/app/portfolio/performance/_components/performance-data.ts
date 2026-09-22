// Chart row/domain building for the performance chart (issue #360 Phase 2,
// issue #377 displayable history). Kept separate from the recharts
// component so merge logic is unit-testable without rendering.

import type {
  BenchmarkCode,
  BenchmarkPerformanceSeries,
  BenchmarkUnavailableReason,
  PortfolioPerformanceSeries,
} from "@/lib/api";
import { toRatio } from "./performance-format";

export const PORTFOLIO_KEY = "portfolio";

export interface ChartSeriesRow {
  date: string;
  portfolio: number | null;
  // Benchmark cumulative % ratio columns, keyed by index_code. Null stays
  // null so the chart cannot bridge an unavailable span.
  [indexCode: string]: string | number | null;
}

export interface BenchmarkPointMeta {
  priceAsOf: string | null;
  fxAsOf: Record<string, string>;
  carried: boolean;
  unavailableReason: BenchmarkUnavailableReason | null;
}

function addUtcDays(isoDate: string, days: number): string {
  const year = Number(isoDate.slice(0, 4));
  const month = Number(isoDate.slice(5, 7));
  const day = Number(isoDate.slice(8, 10));
  const utc = Date.UTC(year, month - 1, day + days);
  return new Date(utc).toISOString().slice(0, 10);
}

function insertMissingCalendarDates(
  byDate: Map<string, ChartSeriesRow>,
  ensureRow: (date: string) => ChartSeriesRow,
): void {
  const valued = [...byDate.values()]
    .filter((row) => typeof row.portfolio === "number" && Number.isFinite(row.portfolio))
    .sort((a, b) => a.date.localeCompare(b.date));
  for (let k = 0; k < valued.length - 1; k += 1) {
    const left = valued[k];
    const right = valued[k + 1];
    if (left === undefined || right === undefined) continue;
    let cursor = addUtcDays(left.date, 1);
    while (cursor < right.date) {
      ensureRow(cursor);
      cursor = addUtcDays(cursor, 1);
    }
  }
}

export interface BuiltChartData {
  rows: ChartSeriesRow[];
  drawnBenchmarks: BenchmarkPerformanceSeries[];
  pointMeta: Record<string, Partial<Record<BenchmarkCode, BenchmarkPointMeta>>>;
}

export function buildChartData(
  portfolio: PortfolioPerformanceSeries | null,
  benchmarks: BenchmarkPerformanceSeries[],
): BuiltChartData {
  const drawnBenchmarks = benchmarks.filter(
    (benchmark) => benchmark.displayable && benchmark.points.some((point) => point.return_pct_cumulative !== null),
  );

  const byDate = new Map<string, ChartSeriesRow>();
  const pointMeta: BuiltChartData["pointMeta"] = {};
  const ensureRow = (date: string): ChartSeriesRow => {
    let row = byDate.get(date);
    if (!row) {
      row = { date, portfolio: null };
      byDate.set(date, row);
    }
    return row;
  };

  if (portfolio && !portfolio.empty) {
    for (const point of portfolio.points) {
      const row = ensureRow(point.date);
      row.portfolio = toRatio(point.return_pct_cumulative);
    }
  }

  for (const benchmark of drawnBenchmarks) {
    const code = benchmark.index_code;
    for (const point of benchmark.points) {
      const row = ensureRow(point.date);
      row[code] = toRatio(point.return_pct_cumulative);
      const metaForDate = pointMeta[point.date] ?? {};
      metaForDate[code] = {
        priceAsOf: point.price_as_of,
        fxAsOf: point.fx_as_of,
        carried: point.carried,
        unavailableReason: point.unavailable_reason,
      };
      pointMeta[point.date] = metaForDate;
    }
  }

  insertMissingCalendarDates(byDate, ensureRow);

  const rows = [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));

  return {
    rows,
    drawnBenchmarks,
    pointMeta,
  };
}

export function hasApproximateSegment(portfolio: PortfolioPerformanceSeries | null): boolean {
  return (
    portfolio !== null &&
    !portfolio.empty &&
    portfolio.points.some((point) => point.is_approximate)
  );
}

export function seriesHasSingleValue(rows: readonly ChartSeriesRow[], key: string): boolean {
  let count = 0;
  for (const row of rows) {
    const value = row[key];
    if (typeof value === "number" && Number.isFinite(value)) count += 1;
    if (count > 1) return false;
  }
  return count === 1;
}
