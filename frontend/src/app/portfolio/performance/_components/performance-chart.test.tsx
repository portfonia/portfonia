import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { ChartSeriesRow } from "./performance-data";
import { PerformanceChart, type ChartSeriesSpec } from "./performance-chart";

// The shared vitest ResizeObserver stub (vitest.setup.ts) exists only so
// recharts' ResponsiveContainer can MOUNT — it never reports a size, so
// charts render 0x0 and no SVG curves exist to assert. This suite needs
// real geometry: report a fixed 800x300 contentRect on observe() so the
// LineChart actually draws its paths (breakdown-chart.test.tsx never
// asserts rendered SVG, which is why the shared stub is enough there).
class SizedResizeObserver {
  private callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
  }
  observe(target: Element): void {
    const entry = {
      target,
      contentRect: {
        x: 0,
        y: 0,
        width: 800,
        height: 300,
        top: 0,
        right: 800,
        bottom: 300,
        left: 0,
        toJSON: () => ({}),
      },
    } as ResizeObserverEntry;
    this.callback([entry], this as unknown as ResizeObserver);
  }
  unobserve(): void {}
  disconnect(): void {}
}
if (typeof globalThis.ResizeObserver !== "undefined") {
  (globalThis as { ResizeObserver: unknown }).ResizeObserver = SizedResizeObserver;
}

const PORTFOLIO_COLOR = "var(--chart-1)";
const SP500_COLOR = "var(--chart-2)";

function seriesFor(includeApprox: boolean): ChartSeriesSpec[] {
  const series: ChartSeriesSpec[] = [
    { key: "portfolio", label: "Portfolio", color: PORTFOLIO_COLOR, isPortfolio: true },
  ];
  if (includeApprox) {
    series.push({
      key: "portfolioApprox",
      label: "Portfolio",
      color: PORTFOLIO_COLOR,
      dashed: true,
      isPortfolio: true,
    });
  }
  series.push({ key: "sp500", label: "S&P 500", color: SP500_COLOR });
  return series;
}

function renderChart(rows: ChartSeriesRow[], singlePortfolioPoint: boolean) {
  const includeApprox = rows.some((row) => row.portfolioApprox !== null);
  return render(
    <LocaleProvider>
      <PerformanceChart
        rows={rows}
        series={seriesFor(includeApprox)}
        singlePortfolioPoint={singlePortfolioPoint}
      />
    </LocaleProvider>,
  );
}

describe("PerformanceChart", () => {
  it("draws a solid portfolio line thicker than benchmark lines", () => {
    const rows = [
      { date: "2026-08-03", portfolio: 0, portfolioApprox: null, sp500: 0 },
      { date: "2026-08-04", portfolio: 0.1, portfolioApprox: null, sp500: 0.02 },
      { date: "2026-08-05", portfolio: 0.05, portfolioApprox: null, sp500: 0.01 },
    ];
    const { container } = renderChart(rows, false);

    const curves = [...container.querySelectorAll("path.recharts-line-curve")];
    const portfolioCurve = curves.find((path) => path.getAttribute("stroke-width") === "2.5");
    const benchmarkCurve = curves.find((path) => path.getAttribute("stroke-width") === "1.5");
    expect(portfolioCurve).toBeDefined();
    expect(benchmarkCurve).toBeDefined();
    // No approximate segment here, so no dashed stroke anywhere.
    expect(container.querySelector('path[stroke-dasharray]')).toBeNull();
  });

  it("renders approximate segments as dashed strokes (req 6)", () => {
    const rows = [
      { date: "2026-08-03", portfolio: 0, portfolioApprox: null, sp500: 0 },
      { date: "2026-08-04", portfolio: null, portfolioApprox: 0.1, sp500: 0.02 },
      { date: "2026-08-05", portfolio: null, portfolioApprox: 0.15, sp500: 0.01 },
    ];
    const { container } = renderChart(rows, false);

    expect(container.querySelector('path[stroke-dasharray="5 4"]')).not.toBeNull();
  });

  it("renders a dot when the portfolio has a single point (short history)", () => {
    const rows = [{ date: "2026-08-03", portfolio: 0, portfolioApprox: null, sp500: 0.02 }];
    const { container } = renderChart(rows, true);

    expect(container.querySelector(".recharts-line-dots")).not.toBeNull();
  });

  it("renders nothing for empty rows", () => {
    const { container } = renderChart([], false);
    expect(container.querySelector(".recharts-responsive-container")).toBeNull();
  });
});
