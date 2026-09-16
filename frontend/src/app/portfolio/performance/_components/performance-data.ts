// Chart row/domain building for the performance chart (issue #360 Phase 2,
// issue #377 displayable history, issue #486/#493 dashed connectors). Kept
// separate from the recharts component so merge logic is unit-testable
// without rendering.

import type {
  BenchmarkCode,
  BenchmarkPerformanceSeries,
  BenchmarkUnavailableReason,
  PortfolioPerformanceSeries,
} from "@/lib/api";
import { toRatio } from "./performance-format";

export const PORTFOLIO_KEY = "portfolio";
export const PORTFOLIO_APPROX_KEY = "portfolioApprox";
export const PORTFOLIO_GAP_KEY = "portfolioGap";

export function portfolioGapSeriesKey(index: number): string {
  return index === 0 ? PORTFOLIO_GAP_KEY : `${PORTFOLIO_GAP_KEY}:${index}`;
}

export function isPortfolioGapKey(key: string): boolean {
  return key === PORTFOLIO_GAP_KEY || key.startsWith(`${PORTFOLIO_GAP_KEY}:`);
}

export function portfolioGapSeriesKeys(rows: readonly ChartSeriesRow[]): string[] {
  const keys: string[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!isPortfolioGapKey(key) || seen.has(key)) continue;
      const value = row[key];
      if (typeof value === "number" && Number.isFinite(value)) {
        seen.add(key);
        keys.push(key);
      }
    }
  }
  return keys.sort((a, b) => gapKeyIndex(a) - gapKeyIndex(b));
}

function gapKeyIndex(key: string): number {
  if (key === PORTFOLIO_GAP_KEY) return 0;
  const parsed = Number(key.slice(PORTFOLIO_GAP_KEY.length + 1));
  return Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY;
}

export interface ChartSeriesRow {
  date: string;
  portfolio: number | null;
  portfolioApprox: number | null;
  // Rendering-only dashed connector (issues #486/#493). Holds already-real
  // boundary and approximate-run values; missing calendar dates stay null
  // and are never reconstructed.
  portfolioGap: number | null;
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

export function rowPortfolioValue(row: ChartSeriesRow): number | null {
  if (typeof row.portfolio === "number" && Number.isFinite(row.portfolio)) {
    return row.portfolio;
  }
  if (typeof row.portfolioApprox === "number" && Number.isFinite(row.portfolioApprox)) {
    return row.portfolioApprox;
  }
  return null;
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
    .filter((row) => rowPortfolioValue(row) !== null)
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

function isSolidEligible(row: ChartSeriesRow): boolean {
  return typeof row.portfolio === "number" && Number.isFinite(row.portfolio);
}

function fillPortfolioGapColumns(rows: ChartSeriesRow[]): void {
  // One independent dashed column per maximal run of consecutive
  // non-solid-eligible dates (approximate points or missing calendar
  // days), bounded by the adjacent real values. Issue #493.
  let i = 0;
  let gapIndex = 0;
  while (i < rows.length) {
    const startRow = rows[i];
    if (startRow === undefined || isSolidEligible(startRow)) {
      i += 1;
      continue;
    }
    const lo = i;
    let hi = i;
    while (hi + 1 < rows.length) {
      const next = rows[hi + 1];
      if (next === undefined || isSolidEligible(next)) break;
      hi += 1;
    }

    const points: { row: ChartSeriesRow; value: number }[] = [];
    const leftRow = lo > 0 ? rows[lo - 1] : undefined;
    const leftVal = leftRow === undefined ? null : rowPortfolioValue(leftRow);
    if (leftRow !== undefined && leftVal !== null) {
      points.push({ row: leftRow, value: leftVal });
    }
    for (let k = lo; k <= hi; k += 1) {
      const interior = rows[k];
      if (interior === undefined) continue;
      const value = rowPortfolioValue(interior);
      if (value !== null) points.push({ row: interior, value });
    }
    const rightRow = hi + 1 < rows.length ? rows[hi + 1] : undefined;
    const rightVal = rightRow === undefined ? null : rowPortfolioValue(rightRow);
    if (rightRow !== undefined && rightVal !== null) {
      points.push({ row: rightRow, value: rightVal });
    }

    if (points.length >= 2) {
      const key = portfolioGapSeriesKey(gapIndex);
      for (const point of points) {
        point.row[key] = point.value;
      }
      gapIndex += 1;
    }
    i = hi + 1;
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

  const portfolioColumns = portfolio && !portfolio.empty ? splitPortfolioColumns(portfolio) : null;

  const byDate = new Map<string, ChartSeriesRow>();
  const pointMeta: BuiltChartData["pointMeta"] = {};
  const ensureRow = (date: string): ChartSeriesRow => {
    let row = byDate.get(date);
    if (!row) {
      row = { date, portfolio: null, portfolioApprox: null, portfolioGap: null };
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

  insertMissingCalendarDates(byDate, ensureRow);

  const rows = [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
  fillPortfolioGapColumns(rows);

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
