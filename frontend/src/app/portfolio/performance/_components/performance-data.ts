// Chart row/domain building for the performance chart (issue #360 Phase 2,
// issue #377 displayable history, issue #486 gap connector). Kept separate
// from the recharts component so merge logic is unit-testable without rendering.

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
  // Rendering-only connector between two already-real points across a
  // missing-date run (issue #486). Null everywhere except those two
  // boundaries; never a reconstructed value.
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

function rowPortfolioValue(row: ChartSeriesRow): number | null {
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

function fillPortfolioGapColumns(rows: ChartSeriesRow[]): void {
  const values = rows.map(rowPortfolioValue);
  let i = 0;
  let gapIndex = 0;
  while (i < rows.length) {
    if (values[i] === null) {
      i += 1;
      continue;
    }
    let j = i + 1;
    while (j < rows.length && values[j] === null) {
      j += 1;
    }
    if (j >= rows.length) {
      break;
    }
    if (j > i + 1) {
      const left = values[i];
      const right = values[j];
      const leftRow = rows[i];
      const rightRow = rows[j];
      if (left === null || right === null || leftRow === undefined || rightRow === undefined) {
        i = j;
        continue;
      }
      const key = portfolioGapSeriesKey(gapIndex);
      leftRow[key] = left;
      rightRow[key] = right;
      gapIndex += 1;
    }
    i = j;
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
