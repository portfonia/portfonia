import { describe, expect, it } from "vitest";

import type {
  BenchmarkPerformanceSeries,
  PortfolioPerformanceSeries,
} from "@/lib/api";
import {
  buildChartData,
  hasApproximateSegment,
} from "./performance-data";
import { formatSignedPct, formatTickPct, toRatio } from "./performance-format";

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

  it("puts each portfolio point in the portfolio column regardless of is_approximate", () => {
    const portfolio = portfolioSeries([
      { date: "2026-08-03", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
      { date: "2026-08-04", value_base: "110", return_pct_cumulative: "0.1", is_approximate: true },
      { date: "2026-08-05", value_base: "121", return_pct_cumulative: "0.21", is_approximate: true },
      { date: "2026-08-06", value_base: "120", return_pct_cumulative: "0.2", is_approximate: false },
    ]);

    const { rows } = buildChartData(portfolio, []);

    expect(rows.map((r) => r.portfolio)).toEqual([0, 0.1, 0.21, 0.2]);
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
    expect(rows[2]?.portfolio).toBeNull();
    expect(rows[0]?.portfolio).toBe(0);
    expect(rows[3]?.portfolio).toBe(0.1);
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

  it("renders axis ticks with exactly two decimal places", () => {
    expect(formatTickPct(0.0323)).toBe("3.23%");
    expect(formatTickPct(-0.0000001)).toBe("0.00%");
    expect(formatTickPct(0)).toBe("0.00%");
  });
});
