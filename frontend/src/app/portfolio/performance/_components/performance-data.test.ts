import { describe, expect, it } from "vitest";

import type {
  BenchmarkPerformanceSeries,
  PortfolioPerformanceSeries,
} from "@/lib/api";
import {
  buildChartData,
  hasApproximateSegment,
  type ChartSeriesRow,
} from "./performance-data";
import { formatSignedPct, toRatio } from "./performance-format";

function portfolioSeries(points: PortfolioPerformanceSeries["points"]): PortfolioPerformanceSeries {
  return {
    empty: false,
    start_date: points[0]?.date ?? null,
    end_date: points.at(-1)?.date ?? null,
    tracking_start: points[0]?.date ?? null,
    points,
    quality_flags: [],
  };
}

function benchmarkPoint(
  date: string,
  return_pct_cumulative: string | null,
  extras: Partial<BenchmarkPerformanceSeries["points"][number]> = {},
): BenchmarkPerformanceSeries["points"][number] {
  return {
    date,
    return_pct_cumulative,
    price_as_of: return_pct_cumulative === null ? null : date,
    fx_as_of: {},
    carried: false,
    unavailable_reason: null,
    ...extras,
  };
}

function benchmarkSeries(
  index_code: BenchmarkPerformanceSeries["index_code"],
  points: BenchmarkPerformanceSeries["points"],
  extras: Partial<BenchmarkPerformanceSeries> = {},
): BenchmarkPerformanceSeries {
  const nonNull = points.filter((point) => point.return_pct_cumulative !== null);
  return {
    index_code,
    name: index_code,
    start_date: points[0]?.date ?? null,
    points,
    comparable: extras.comparable ?? true,
    displayable: extras.displayable ?? nonNull.length > 0,
    normalization: extras.normalization ?? "portfolio_start",
    anchor_date: extras.anchor_date ?? nonNull[0]?.date ?? null,
    display_start_date: extras.display_start_date ?? nonNull[0]?.date ?? null,
    display_end_date: extras.display_end_date ?? nonNull.at(-1)?.date ?? null,
    comparison_start: extras.comparison_start ?? null,
    comparison_end: extras.comparison_end ?? null,
    comparison_status: extras.comparison_status ?? "available",
    comparison_return_pct: extras.comparison_return_pct ?? null,
    ...extras,
  };
}

describe("buildChartData", () => {
  it("merges portfolio and drawn benchmarks into date-sorted rows", () => {
    const portfolio = portfolioSeries([
      { date: "2026-08-03", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-08-04", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
    ]);
    const sp500 = benchmarkSeries("sp500", [
      benchmarkPoint("2026-08-03", "0"),
      benchmarkPoint("2026-08-04", "0.02"),
    ]);

    const { rows, drawnBenchmarks } = buildChartData(portfolio, [sp500]);

    expect(drawnBenchmarks.map((b) => b.index_code)).toEqual(["sp500"]);
    expect(rows.map((row) => row.date)).toEqual(["2026-08-03", "2026-08-04"]);
    expect(rows[0]).toMatchObject({ portfolio: 0, sp500: 0 });
    expect(rows[1]).toMatchObject({ portfolio: 0.1, sp500: 0.02 });
  });

  it("draws non-comparable history when displayable, and keeps nulls as null", () => {
    const portfolio = portfolioSeries([
      { date: "2026-08-03", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
    ]);
    const dow30 = benchmarkSeries("dow30", [], { comparable: false, displayable: false });
    const nasdaq = benchmarkSeries(
      "nasdaq",
      [benchmarkPoint("2026-08-03", "0"), benchmarkPoint("2026-08-04", null, { unavailable_reason: "missing_price" })],
      { comparable: false, comparison_status: "anchor_unavailable", normalization: "own_start" },
    );

    const { rows, drawnBenchmarks } = buildChartData(portfolio, [dow30, nasdaq]);

    expect(drawnBenchmarks.map((b) => b.index_code)).toEqual(["nasdaq"]);
    expect(rows[0]).toMatchObject({ portfolio: 0, nasdaq: 0 });
    expect(rows[1]).toMatchObject({ nasdaq: null });
    expect(rows[0].dow30).toBeUndefined();
  });

  it("keeps benchmark dates outside the portfolio window in the rows", () => {
    const portfolio = portfolioSeries([
      { date: "2026-08-04", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
    ]);
    const sp500 = benchmarkSeries("sp500", [benchmarkPoint("2026-08-03", "0")]);

    const { rows } = buildChartData(portfolio, [sp500]);
    expect(rows.map((row) => row.date)).toEqual(["2026-08-03", "2026-08-04"]);
  });

  it("splits approximate portfolio points into the dashed column (issue #360 req 6)", () => {
    const portfolio = portfolioSeries([
      { date: "2026-08-03", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-08-04", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
      { date: "2026-08-05", value_base: "121", return_pct_cumulative: "0.21", is_approximate: true },
      { date: "2026-08-06", value_base: "130", return_pct_cumulative: "0.3", is_approximate: true },
      { date: "2026-08-07", value_base: "120", return_pct_cumulative: "0.2", is_approximate: false },
    ]);

    const { rows } = buildChartData(portfolio, []);

    const row: Record<string, ChartSeriesRow> = Object.fromEntries(
      rows.map((r) => [r.date, r]),
    );
    expect(row["2026-08-03"].portfolio).toBe(0);
    expect(row["2026-08-04"].portfolio).toBe(0.1);
    // Approximate days carry no solid value and vice versa — one column per
    // point, so an approximate stretch reads as a separate dashed segment.
    expect(row["2026-08-05"].portfolio).toBeNull();
    expect(row["2026-08-05"].portfolioApprox).toBe(0.21);
    expect(row["2026-08-06"].portfolioApprox).toBe(0.3);
    expect(row["2026-08-06"].portfolio).toBeNull();
    expect(row["2026-08-07"].portfolio).toBe(0.2);
    expect(row["2026-08-07"].portfolioApprox).toBeNull();
    // Approximate stretch is a dashed-eligible run: connector uses each
    // day's own real value plus the bounding solid endpoints (#493).
    expect(row["2026-08-03"].portfolioGap).toBeNull();
    expect(row["2026-08-04"].portfolioGap).toBe(0.1);
    expect(row["2026-08-05"].portfolioGap).toBe(0.21);
    expect(row["2026-08-06"].portfolioGap).toBe(0.3);
    expect(row["2026-08-07"].portfolioGap).toBe(0.2);
  });

  it("draws a dashed connector across a solid-to-approximate transition (#493)", () => {
    const portfolio = portfolioSeries([
      { date: "2026-08-03", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-08-04", value_base: "110", return_pct_cumulative: "0.1", is_approximate: true },
    ]);

    const { rows } = buildChartData(portfolio, []);
    const row: Record<string, ChartSeriesRow> = Object.fromEntries(
      rows.map((r) => [r.date, r]),
    );

    expect(row["2026-08-03"].portfolio).toBe(0);
    expect(row["2026-08-03"].portfolioApprox).toBeNull();
    expect(row["2026-08-04"].portfolio).toBeNull();
    expect(row["2026-08-04"].portfolioApprox).toBe(0.1);
    expect(row["2026-08-03"].portfolioGap).toBe(0);
    expect(row["2026-08-04"].portfolioGap).toBe(0.1);
  });

  it("sets portfolioGap only at the two real boundaries of a missing-date run (#486)", () => {
    // Day 1 real, days 2-3 absent from the portfolio series (weekend / capture
    // hole) but present as chart rows via the benchmark, day 4 real.
    const portfolio = portfolioSeries([
      { date: "2026-09-11", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-09-14", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
    ]);
    const sp500 = benchmarkSeries("sp500", [
      benchmarkPoint("2026-09-11", "0"),
      benchmarkPoint("2026-09-12", "0.01"),
      benchmarkPoint("2026-09-13", "0.012"),
      benchmarkPoint("2026-09-14", "0.02"),
    ]);

    const { rows } = buildChartData(portfolio, [sp500]);
    const row: Record<string, ChartSeriesRow> = Object.fromEntries(
      rows.map((r) => [r.date, r]),
    );

    expect(row["2026-09-11"].portfolioGap).toBe(0);
    expect(row["2026-09-11"].portfolio).toBe(0);
    expect(row["2026-09-12"].portfolioGap).toBeNull();
    expect(row["2026-09-12"].portfolio).toBeNull();
    expect(row["2026-09-12"].portfolioApprox).toBeNull();
    expect(row["2026-09-13"].portfolioGap).toBeNull();
    expect(row["2026-09-13"].portfolio).toBeNull();
    expect(row["2026-09-13"].portfolioApprox).toBeNull();
    expect(row["2026-09-14"].portfolioGap).toBe(0.1);
    expect(row["2026-09-14"].portfolio).toBe(0.1);
  });

  it("uses an approximate boundary's own value for portfolioGap (#486)", () => {
    const portfolio = portfolioSeries([
      { date: "2026-09-11", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-09-14", value_base: "110", return_pct_cumulative: "0.1", is_approximate: true },
    ]);
    const sp500 = benchmarkSeries("sp500", [
      benchmarkPoint("2026-09-11", "0"),
      benchmarkPoint("2026-09-12", "0.01"),
      benchmarkPoint("2026-09-13", "0.012"),
      benchmarkPoint("2026-09-14", "0.02"),
    ]);

    const { rows } = buildChartData(portfolio, [sp500]);
    const row: Record<string, ChartSeriesRow> = Object.fromEntries(
      rows.map((r) => [r.date, r]),
    );

    expect(row["2026-09-11"].portfolioGap).toBe(0);
    expect(row["2026-09-14"].portfolioGap).toBe(0.1);
    expect(row["2026-09-14"].portfolioApprox).toBe(0.1);
    expect(row["2026-09-14"].portfolio).toBeNull();
    expect(row["2026-09-12"].portfolioGap).toBeNull();
    expect(row["2026-09-13"].portfolioGap).toBeNull();
  });

  it("leaves portfolioGap null on every row when there are no missing dates (#486)", () => {
    const portfolio = portfolioSeries([
      { date: "2026-09-11", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-09-12", value_base: "105", return_pct_cumulative: "0.05", is_approximate: false },
      { date: "2026-09-13", value_base: "108", return_pct_cumulative: "0.08", is_approximate: false },
    ]);
    const sp500 = benchmarkSeries("sp500", [
      benchmarkPoint("2026-09-11", "0"),
      benchmarkPoint("2026-09-12", "0.01"),
      benchmarkPoint("2026-09-13", "0.02"),
    ]);

    const { rows } = buildChartData(portfolio, [sp500]);
    expect(rows.map((r) => r.portfolioGap)).toEqual([null, null, null]);
  });

  it("keeps an approximate stretch on its own connector and does not drop its values (#493)", () => {
    const portfolio = portfolioSeries([
      { date: "2026-08-03", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-08-04", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
      { date: "2026-08-05", value_base: "121", return_pct_cumulative: "0.21", is_approximate: true },
      { date: "2026-08-06", value_base: "130", return_pct_cumulative: "0.3", is_approximate: true },
      { date: "2026-08-07", value_base: "120", return_pct_cumulative: "0.2", is_approximate: false },
    ]);

    const { rows } = buildChartData(portfolio, []);
    expect(rows.map((r) => r.portfolioApprox)).toEqual([null, null, 0.21, 0.3, null]);
    expect(rows.map((r) => r.portfolioGap)).toEqual([null, 0.1, 0.21, 0.3, 0.2]);
    expect(rows.map((r) => r.portfolio)).toEqual([0, 0.1, null, null, 0.2]);
  });

  it("does not put two non-solid runs separated by a solid stretch on one connector (#493)", () => {
    const portfolio = portfolioSeries([
      { date: "2026-08-03", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-08-04", value_base: "101", return_pct_cumulative: "0.01", is_approximate: true },
      { date: "2026-08-05", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
      { date: "2026-08-06", value_base: "120", return_pct_cumulative: "0.2", is_approximate: false },
      { date: "2026-08-07", value_base: "121", return_pct_cumulative: "0.21", is_approximate: true },
      { date: "2026-08-08", value_base: "130", return_pct_cumulative: "0.3", is_approximate: false },
    ]);

    const { rows } = buildChartData(portfolio, []);
    const row: Record<string, ChartSeriesRow> = Object.fromEntries(
      rows.map((r) => [r.date, r]),
    );

    expect(row["2026-08-03"].portfolioGap).toBe(0);
    expect(row["2026-08-04"].portfolioGap).toBe(0.01);
    expect(row["2026-08-05"].portfolioGap).toBe(0.1);
    expect(row["2026-08-05"]["portfolioGap:1"] ?? null).toBeNull();
    expect(row["2026-08-06"].portfolioGap).toBeNull();
    expect(row["2026-08-06"]["portfolioGap:1"]).toBe(0.2);
    expect(row["2026-08-07"]["portfolioGap:1"]).toBe(0.21);
    expect(row["2026-08-08"]["portfolioGap:1"]).toBe(0.3);
    expect(row["2026-08-08"].portfolioGap).toBeNull();
  });

  it("assigns independent boundary values for multiple separate gaps (#486)", () => {
    const portfolio = portfolioSeries([
      { date: "2026-09-11", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-09-14", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
      { date: "2026-09-17", value_base: "121", return_pct_cumulative: "0.21", is_approximate: false },
    ]);
    const sp500 = benchmarkSeries("sp500", [
      benchmarkPoint("2026-09-11", "0"),
      benchmarkPoint("2026-09-12", "0.01"),
      benchmarkPoint("2026-09-13", "0.012"),
      benchmarkPoint("2026-09-14", "0.02"),
      benchmarkPoint("2026-09-15", "0.021"),
      benchmarkPoint("2026-09-16", "0.022"),
      benchmarkPoint("2026-09-17", "0.03"),
    ]);

    const { rows } = buildChartData(portfolio, [sp500]);
    const row: Record<string, ChartSeriesRow> = Object.fromEntries(
      rows.map((r) => [r.date, r]),
    );

    // Each gap is its own column so Recharts cannot join them through the
    // shared middle boundary (acceptance 5).
    expect(row["2026-09-11"].portfolioGap).toBe(0);
    expect(row["2026-09-12"].portfolioGap).toBeNull();
    expect(row["2026-09-13"].portfolioGap).toBeNull();
    expect(row["2026-09-14"].portfolioGap).toBe(0.1);
    expect(row["2026-09-14"]["portfolioGap:1"]).toBe(0.1);
    expect(row["2026-09-15"].portfolioGap).toBeNull();
    expect(row["2026-09-15"]["portfolioGap:1"] ?? null).toBeNull();
    expect(row["2026-09-16"].portfolioGap).toBeNull();
    expect(row["2026-09-17"].portfolioGap).toBeNull();
    expect(row["2026-09-17"]["portfolioGap:1"]).toBe(0.21);
    expect(row["2026-09-11"]["portfolioGap:1"] ?? null).toBeNull();
  });

  it("does not put two gaps separated by real history on one connector column (#486)", () => {
    const portfolio = portfolioSeries([
      { date: "2026-09-11", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-09-14", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
      { date: "2026-09-15", value_base: "150", return_pct_cumulative: "0.5", is_approximate: false },
      { date: "2026-09-16", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
      { date: "2026-09-17", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
      { date: "2026-09-18", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
      { date: "2026-09-21", value_base: "120", return_pct_cumulative: "0.2", is_approximate: false },
    ]);
    const sp500 = benchmarkSeries("sp500", [
      "2026-09-11",
      "2026-09-12",
      "2026-09-13",
      "2026-09-14",
      "2026-09-15",
      "2026-09-16",
      "2026-09-17",
      "2026-09-18",
      "2026-09-19",
      "2026-09-20",
      "2026-09-21",
    ].map((date) => benchmarkPoint(date, "0")));

    const { rows } = buildChartData(portfolio, [sp500]);
    const row: Record<string, ChartSeriesRow> = Object.fromEntries(
      rows.map((r) => [r.date, r]),
    );

    expect(row["2026-09-11"].portfolioGap).toBe(0);
    expect(row["2026-09-14"].portfolioGap).toBe(0.1);
    expect(row["2026-09-14"]["portfolioGap:1"] ?? null).toBeNull();
    expect(row["2026-09-15"].portfolioGap).toBeNull();
    expect(row["2026-09-15"]["portfolioGap:1"] ?? null).toBeNull();
    expect(row["2026-09-18"].portfolioGap).toBeNull();
    expect(row["2026-09-18"]["portfolioGap:1"]).toBe(0.1);
    expect(row["2026-09-21"]["portfolioGap:1"]).toBe(0.2);
    expect(row["2026-09-21"].portfolioGap).toBeNull();
  });

  it("inserts calendar holes in portfolio-only mode so a weekend is a gap (#486)", () => {
    const portfolio = portfolioSeries([
      { date: "2026-09-11", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-09-14", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
    ]);

    const { rows } = buildChartData(portfolio, []);
    expect(rows.map((r) => r.date)).toEqual([
      "2026-09-11",
      "2026-09-12",
      "2026-09-13",
      "2026-09-14",
    ]);
    expect(rows[1]?.portfolio).toBeNull();
    expect(rows[1]?.portfolioApprox).toBeNull();
    expect(rows[2]?.portfolio).toBeNull();
    expect(rows[0]?.portfolioGap).toBe(0);
    expect(rows[3]?.portfolioGap).toBe(0.1);
  });

  it("still detects a calendar gap when the only benchmark is not displayable (#486)", () => {
    const portfolio = portfolioSeries([
      { date: "2026-09-11", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-09-14", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
    ]);
    const dow30 = benchmarkSeries("dow30", [], { comparable: false, displayable: false });

    const { rows } = buildChartData(portfolio, [dow30]);
    expect(rows.map((r) => r.date)).toEqual([
      "2026-09-11",
      "2026-09-12",
      "2026-09-13",
      "2026-09-14",
    ]);
    expect(rows[0]?.portfolioGap).toBe(0);
    expect(rows[3]?.portfolioGap).toBe(0.1);
  });

  it("returns no rows when the portfolio is empty and no benchmark draws", () => {
    const empty: PortfolioPerformanceSeries = {
      empty: true,
      start_date: null,
      end_date: null,
      tracking_start: null,
      points: [],
      quality_flags: [],
    };
    const { rows } = buildChartData(empty, []);
    expect(rows).toEqual([]);
  });
});

describe("hasApproximateSegment", () => {
  it("is false for null, empty, or all-solid series", () => {
    expect(hasApproximateSegment(null)).toBe(false);
    const empty: PortfolioPerformanceSeries = {
      empty: true,
      start_date: null,
      end_date: null,
      tracking_start: null,
      points: [],
      quality_flags: [],
    };
    expect(hasApproximateSegment(empty)).toBe(false);
    const solid = portfolioSeries([
      { date: "2026-08-03", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
    ]);
    expect(hasApproximateSegment(solid)).toBe(false);
  });

  it("is true when any point is approximate", () => {
    const mixed = portfolioSeries([
      { date: "2026-08-03", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-08-04", value_base: "101", return_pct_cumulative: "0.01", is_approximate: true },
    ]);
    expect(hasApproximateSegment(mixed)).toBe(true);
  });
});

describe("performance-format", () => {
  it("parses wire ratio strings and renders signed percentages", () => {
    expect(toRatio("0.0234")).toBe(0.0234);
    expect(toRatio(null)).toBeNull();
    expect(toRatio("")).toBeNull();
    expect(formatSignedPct(0.0234)).toBe("+2.34%");
    expect(formatSignedPct(-0.1)).toBe("-10.00%");
    expect(formatSignedPct(0)).toBe("+0.00%");
    expect(formatSignedPct(null)).toBe("—");
  });
});
