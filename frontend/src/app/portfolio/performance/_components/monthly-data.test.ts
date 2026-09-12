import { describe, expect, it } from "vitest";

import type { MonthlyPerformance } from "@/lib/api";
import { MONTHLY_CHART_CURSOR, adaptiveMonthTicks, buildMonthlyRows } from "./monthly-data";

function monthlyPoint(
  month: string,
  overrides: Partial<MonthlyPerformance["points"][number]> = {},
): MonthlyPerformance["points"][number] {
  return {
    month,
    start_date: `${month}-01`,
    end_date: `${month}-28`,
    portfolio_return_pct: "0.0100",
    benchmark_return_pct: "0.0050",
    partial_reason: null,
    is_approximate: false,
    benchmark_unavailable_reason: null,
    ...overrides,
  };
}

describe("buildMonthlyRows", () => {
  it("returns an empty array for a null series", () => {
    expect(buildMonthlyRows(null)).toEqual([]);
  });

  it("maps ratio strings to numbers and passes through partial/approximate state", () => {
    const monthly: MonthlyPerformance = {
      method: "approx_eod_twr",
      benchmark_code: "sp500",
      points: [
        monthlyPoint("2026-09", { portfolio_return_pct: "-0.0100", partial_reason: "tracking_start" }),
      ],
    };
    const rows = buildMonthlyRows(monthly);
    expect(rows).toEqual([
      {
        month: "2026-09",
        startDate: "2026-09-01",
        endDate: "2026-09-28",
        portfolio: -0.01,
        benchmark: 0.005,
        partialReason: "tracking_start",
        isApproximate: false,
        benchmarkUnavailableReason: null,
      },
    ]);
  });

  it("keeps a null benchmark null instead of coercing to 0", () => {
    const monthly: MonthlyPerformance = {
      method: "approx_eod_twr",
      benchmark_code: "sp500",
      points: [
        monthlyPoint("2026-09", {
          benchmark_return_pct: null,
          benchmark_unavailable_reason: "missing_price",
        }),
      ],
    };
    const rows = buildMonthlyRows(monthly);
    expect(rows[0].benchmark).toBeNull();
    expect(rows[0].benchmarkUnavailableReason).toBe("missing_price");
  });
});

describe("adaptiveMonthTicks", () => {
  it("labels every month when the series is short (6M/1Y range)", () => {
    const months = Array.from({ length: 6 }, (_, i) => `2026-0${i + 1}`);
    expect(adaptiveMonthTicks(months)).toEqual(months);
  });

  it("thins labels for a long series (5Y/ALL range) without exceeding the cap", () => {
    const months = Array.from({ length: 60 }, (_, i) => {
      const year = 2021 + Math.floor(i / 12);
      const month = String((i % 12) + 1).padStart(2, "0");
      return `${year}-${month}`;
    });
    const ticks = adaptiveMonthTicks(months);
    expect(ticks.length).toBeLessThanOrEqual(13);
    expect(ticks[0]).toBe(months[0]);
    expect(ticks[ticks.length - 1]).toBe(months[months.length - 1]);
  });
});

describe("MONTHLY_CHART_CURSOR", () => {
  // Regression for issue #437: hovering the monthly bar chart used
  // recharts' default (unstyled) Tooltip cursor — an undstyled full-height
  // Rectangle with no fill of its own — which read as a stark, near-white
  // wash over the bars on this app's dark card. The fix is an explicit,
  // theme-aware cursor fill; pin its shape so a future edit can't
  // accidentally drop it back to recharts' default (an absent `cursor`
  // prop, or `cursor={true}`).
  it("sets an explicit, theme-aware, low-opacity fill instead of leaving recharts' default", () => {
    expect(MONTHLY_CHART_CURSOR.fill).toBe("var(--muted-foreground)");
    expect(MONTHLY_CHART_CURSOR.fillOpacity).toBeGreaterThan(0);
    expect(MONTHLY_CHART_CURSOR.fillOpacity).toBeLessThan(1);
  });
});
