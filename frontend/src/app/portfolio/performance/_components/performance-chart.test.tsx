import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { ChartSeriesRow } from "./performance-data";
import { ChartTooltip, PerformanceChart, type ChartSeriesSpec } from "./performance-chart";

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

function seriesFor(singletonPortfolio = false): ChartSeriesSpec[] {
  return [
    {
      key: "portfolio",
      label: "Portfolio",
      color: PORTFOLIO_COLOR,
      isPortfolio: true,
      connectNulls: true,
      singletonDot: singletonPortfolio,
    },
    { key: "sp500", label: "S&P 500", color: SP500_COLOR },
  ];
}

function renderChart(rows: ChartSeriesRow[], singletonPortfolio = false) {
  return render(
    <LocaleProvider>
      <PerformanceChart
        rows={rows}
        series={seriesFor(singletonPortfolio)}
        pointMeta={{}}
        anchorDate={null}
      />
    </LocaleProvider>,
  );
}

function renderTooltip(row: ChartSeriesRow, series: ChartSeriesSpec[]) {
  return render(
    <LocaleProvider>
      <ChartTooltip
        active
        payload={[{ payload: row, graphicalItemId: "portfolio" }]}
        series={series}
        pointMeta={{}}
      />
    </LocaleProvider>,
  );
}

describe("PerformanceChart", () => {
  it("draws a solid portfolio line thicker than benchmark lines", () => {
    const rows = [
      { date: "2026-08-03", portfolio: 0, sp500: 0 },
      { date: "2026-08-04", portfolio: 0.1, sp500: 0.02 },
      { date: "2026-08-05", portfolio: 0.05, sp500: 0.01 },
    ];
    const { container } = renderChart(rows);

    const curves = [...container.querySelectorAll("path.recharts-line-curve")];
    const portfolioCurve = curves.find((path) => path.getAttribute("stroke-width") === "2.5");
    const benchmarkCurve = curves.find((path) => path.getAttribute("stroke-width") === "1.5");
    expect(portfolioCurve).toBeDefined();
    expect(benchmarkCurve).toBeDefined();
  });

  it("renders no dashed stroke on any line", () => {
    const rows = [
      { date: "2026-08-03", portfolio: 0, sp500: 0 },
      { date: "2026-08-04", portfolio: 0.1, sp500: 0.02 },
      { date: "2026-08-05", portfolio: null, sp500: 0.01 },
    ];
    const { container } = renderChart(rows);
    expect(container.querySelector("path[stroke-dasharray]")).toBeNull();
  });

  it("renders a dot when the portfolio has a single point (short history)", () => {
    const rows = [{ date: "2026-08-03", portfolio: 0, sp500: 0.02 }];
    const { container } = renderChart(rows, true);

    expect(container.querySelector(".recharts-line-dots")).not.toBeNull();
  });

  it("shows one portfolio tooltip value on a valued date", () => {
    const rows = [
      { date: "2026-08-03", portfolio: 0, sp500: 0 },
      { date: "2026-08-04", portfolio: 0.1, sp500: 0.01 },
      { date: "2026-08-05", portfolio: 0.21, sp500: 0.03 },
    ];
    renderTooltip(rows[1] as ChartSeriesRow, seriesFor());

    expect(screen.getByText("Portfolio")).toBeInTheDocument();
    expect(screen.getByText("+10.00%")).toBeInTheDocument();
    expect(screen.getByText("S&P 500")).toBeInTheDocument();
    expect(screen.getByText("+1.00%")).toBeInTheDocument();
    expect(screen.getAllByText("Portfolio")).toHaveLength(1);
  });

  it("does not invent a portfolio tooltip value on a missing date (#493)", () => {
    const rows = [
      { date: "2026-08-03", portfolio: 0, sp500: 0 },
      { date: "2026-08-04", portfolio: null, sp500: 0.01 },
      { date: "2026-08-05", portfolio: 0.21, sp500: 0.03 },
    ];
    renderTooltip(rows[1] as ChartSeriesRow, seriesFor());

    expect(screen.queryByText("Portfolio")).toBeNull();
    expect(screen.getByText("S&P 500")).toBeInTheDocument();
    expect(screen.getByText("+1.00%")).toBeInTheDocument();
  });

  it("renders nothing for empty rows", () => {
    const { container } = renderChart([], false);
    expect(container.querySelector(".recharts-responsive-container")).toBeNull();
  });

  it("draws one continuous portfolio line across a null stretch", () => {
    const rows = [
      { date: "2026-09-11", portfolio: 0, sp500: 0 },
      { date: "2026-09-12", portfolio: null, sp500: 0.01 },
      { date: "2026-09-13", portfolio: null, sp500: 0.012 },
      { date: "2026-09-14", portfolio: 0.1, sp500: 0.02 },
    ];
    const { container } = renderChart(rows);

    const portfolioCurve = [...container.querySelectorAll("path.recharts-line-curve")].find(
      (path) => path.getAttribute("stroke-width") === "2.5",
    );
    expect(portfolioCurve).toBeDefined();
    expect(portfolioCurve?.getAttribute("d")?.match(/M/g)?.length).toBe(1);
    expect(portfolioCurve?.getAttribute("stroke-dasharray")).toBeNull();
    expect(container.querySelector(".recharts-dot")).toBeNull();
  });
});
