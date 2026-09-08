// Chart row/domain building for the performance chart (issue #360 Phase 2,
// issue #377 displayable history). Kept separate from the recharts component
// so merge logic is unit-testable without rendering.

import type {
  BenchmarkCode,
  BenchmarkPerformanceSeries,
  BenchmarkUnavailableReason,
  PortfolioPerformanceSeries,
} from "@/lib/api";
import { toRatio } from "./performance-format";

export const PORTFOLIO_KEY = "portfolio";
export const PORTFOLIO_APPROX_KEY = "portfolioApprox";

export interface ChartSeriesRow {
  date: string;
  portfolio: number | null;
  portfolioApprox: number | null;
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

function splitPortfolioColumns(
  portfolio: PortfolioPerformanceSeries,
): Map<string, { solid: number | null; approx: number | null }> {
  const split = new Map<string, { solid: number | null; approx: number | null }>();
  for (const point of portfolio.points) {
    const ratio = toRatio(point.return_pct_cumulative);
    split.set(point.date, {
      solid: !point.is_approximate ? ratio : null,
      approx: point.is_approximate ? ratio : null,
    });
  }
  return split;
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

  const portfolioColumns = portfolio && !portfolio.empty ? splitPortfolioColumns(portfolio) : null;

  const byDate = new Map<string, ChartSeriesRow>();
  const pointMeta: BuiltChartData["pointMeta"] = {};
  const ensureRow = (date: string): ChartSeriesRow => {
    let row = byDate.get(date);
    if (!row) {
      row = { date, portfolio: null, portfolioApprox: null };
      byDate.set(date, row);
    }
    return row;
  };

  if (portfolioColumns) {
    for (const [date, columns] of portfolioColumns) {
      const row = ensureRow(date);
      row.portfolio = columns.solid;
      row.portfolioApprox = columns.approx;
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

  return {
    rows: [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date)),
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
