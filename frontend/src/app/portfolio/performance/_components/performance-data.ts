// Chart row/domain building for the performance chart (issue #360 Phase 2).
// Kept separate from the recharts component so the merge logic (approximate
// segment splitting, common-window benchmark inclusion) is unit-testable
// without rendering.

import type {
  BenchmarkCode,
  BenchmarkPerformanceSeries,
  PortfolioPerformanceSeries,
} from "@/lib/api";
import { toRatio } from "./performance-format";

export const PORTFOLIO_KEY = "portfolio";
export const PORTFOLIO_APPROX_KEY = "portfolioApprox";

export interface ChartSeriesRow {
  date: string;
  portfolio: number | null;
  portfolioApprox: number | null;
  // Benchmark cumulative % ratio columns, keyed by index_code.
  [indexCode: string]: string | number | null;
}

// The portfolio line is drawn as two columns over the same dates so an
// approximate stretch can be dashed while the rest stays solid (issue #360
// requirement 6: approximate segments disclosed). Splitting on each point's
// own `is_approximate` flag means the boundary days of an approximate run
// are each drawn by exactly one column — a solid point never lands in the
// dashed column or vice versa — at the cost of a one-trading-day visual gap
// at the two transition points of a run. That is acceptable: approximate
// stretches (FX-fallback etc.) are contiguous and usually long, and the
// alternative — duplicating transition points into both columns — makes
// recharts draw a solid connector across an approximate day (double-drawn
// segments, style fights on the shared points).
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
  // Benchmarks that actually contribute a line (comparable AND non-empty
  // points) — the chart legend mirrors this, and a requested benchmark that
  // is NOT here is reported to the user as not comparable instead of drawn.
  drawnBenchmarks: BenchmarkPerformanceSeries[];
}

export function buildChartData(
  portfolio: PortfolioPerformanceSeries | null,
  benchmarks: BenchmarkPerformanceSeries[],
): BuiltChartData {
  const drawnBenchmarks = benchmarks.filter(
    (benchmark) => benchmark.comparable && benchmark.points.length > 0,
  );

  const portfolioColumns = portfolio && !portfolio.empty ? splitPortfolioColumns(portfolio) : null;

  const byDate = new Map<string, ChartSeriesRow>();
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
    const code = benchmark.index_code as BenchmarkCode;
    for (const point of benchmark.points) {
      ensureRow(point.date)[code] = toRatio(point.return_pct_cumulative);
    }
  }

  return {
    rows: [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date)),
    drawnBenchmarks,
  };
}

export function hasApproximateSegment(portfolio: PortfolioPerformanceSeries | null): boolean {
  return (
    portfolio !== null &&
    !portfolio.empty &&
    portfolio.points.some((point) => point.is_approximate)
  );
}
