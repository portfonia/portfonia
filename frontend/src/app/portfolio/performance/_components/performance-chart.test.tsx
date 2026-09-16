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

function gapKeysFromRows(rows: ChartSeriesRow[]): string[] {
  const keys = new Set<string>();
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (
        (key === "portfolioGap" || key.startsWith("portfolioGap:")) &&
        typeof row[key] === "number"
      ) {
        keys.add(key);
      }
    }
  }
  return [...keys].sort();
}

function seriesFor(gapKeys: string[] = []): ChartSeriesSpec[] {
  const series: ChartSeriesSpec[] = [];
  // Dashed connectors first so the solid stroke paints on top of them.
  for (const key of gapKeys) {
    series.push({
      key,
      label: "Portfolio",
      color: PORTFOLIO_COLOR,
      dashed: true,
      isPortfolio: true,
      connectNulls: true,
    });
  }
  series.push({
    key: "portfolio",
    label: "Portfolio",
    color: PORTFOLIO_COLOR,
    isPortfolio: true,
  });
  series.push({ key: "sp500", label: "S&P 500", color: SP500_COLOR });
  return series;
}

function renderChart(
  rows: ChartSeriesRow[],
  singletonPortfolio = false,
  extraSeries: ChartSeriesSpec[] = [],
) {
  const gapKeys = gapKeysFromRows(rows);
  const series = [
    ...seriesFor(gapKeys).map((spec) =>
      spec.isPortfolio && !gapKeys.includes(spec.key)
        ? { ...spec, singletonDot: singletonPortfolio }
        : spec,
    ),
    ...extraSeries,
  ];
  return render(
    <LocaleProvider>
      <PerformanceChart rows={rows} series={series} pointMeta={{}} anchorDate={null} />
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
      { date: "2026-08-03", portfolio: 0, portfolioApprox: null, portfolioGap: null, sp500: 0 },
      { date: "2026-08-04", portfolio: 0.1, portfolioApprox: null, portfolioGap: null, sp500: 0.02 },
      { date: "2026-08-05", portfolio: 0.05, portfolioApprox: null, portfolioGap: null, sp500: 0.01 },
    ];
    const { container } = renderChart(rows);

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
      { date: "2026-08-03", portfolio: 0, portfolioApprox: null, portfolioGap: 0, sp500: 0 },
      { date: "2026-08-04", portfolio: null, portfolioApprox: 0.1, portfolioGap: 0.1, sp500: 0.02 },
      { date: "2026-08-05", portfolio: null, portfolioApprox: 0.15, portfolioGap: 0.15, sp500: 0.01 },
    ];
    const { container } = renderChart(rows);

    expect(container.querySelector('path[stroke-dasharray="5 4"]')).not.toBeNull();
  });

  it("renders a dashed connector on a solid-to-approximate transition (#493)", () => {
    const rows = [
      { date: "2026-08-03", portfolio: 0, portfolioApprox: null, portfolioGap: 0, sp500: 0 },
      { date: "2026-08-04", portfolio: null, portfolioApprox: 0.1, portfolioGap: 0.1, sp500: 0.02 },
    ];
    const { container } = renderChart(rows);

    const dashed = [...container.querySelectorAll('path[stroke-dasharray="5 4"]')].filter(
      (path) => path.getAttribute("stroke-width") === "2.5",
    );
    expect(dashed).toHaveLength(1);
    expect(dashed[0]?.getAttribute("d")?.match(/M/g)?.length).toBe(1);
  });

  it("renders a dot when the portfolio has a single point (short history)", () => {
    const rows = [{ date: "2026-08-03", portfolio: 0, portfolioApprox: null, portfolioGap: null, sp500: 0.02 }];
    const { container } = renderChart(rows, true);

    expect(container.querySelector(".recharts-line-dots")).not.toBeNull();
  });

  it("renders a singleton dot for an approximate-only history (#493)", () => {
    const rows = [
      { date: "2026-08-03", portfolio: null, portfolioApprox: 0, portfolioGap: null },
    ];
    const { container } = renderChart(rows, false, [
      {
        key: "portfolioApprox",
        label: "Portfolio",
        color: PORTFOLIO_COLOR,
        dashed: true,
        isPortfolio: true,
        singletonDot: true,
      },
    ]);

    expect(container.querySelector(".recharts-line-dots")).not.toBeNull();
  });

  it("shows one portfolio tooltip value on an approximate date (#493)", () => {
    const rows = [
      { date: "2026-08-03", portfolio: 0, portfolioApprox: null, portfolioGap: 0, sp500: 0 },
      { date: "2026-08-04", portfolio: null, portfolioApprox: 0.1, portfolioGap: 0.1, sp500: 0.01 },
      { date: "2026-08-05", portfolio: 0.21, portfolioApprox: null, portfolioGap: 0.21, sp500: 0.03 },
    ];
    // Same series shape the page emits: connectors + solid, no portfolioApprox line.
    renderTooltip(rows[1] as ChartSeriesRow, seriesFor(gapKeysFromRows(rows)));

    expect(screen.getByText("Portfolio")).toBeInTheDocument();
    expect(screen.getByText("+10.00%")).toBeInTheDocument();
    expect(screen.getByText("S&P 500")).toBeInTheDocument();
    expect(screen.getByText("+1.00%")).toBeInTheDocument();
    expect(screen.getAllByText("Portfolio")).toHaveLength(1);
  });

  it("does not invent a portfolio tooltip value on a missing date (#493)", () => {
    const rows = [
      { date: "2026-08-03", portfolio: 0, portfolioApprox: null, portfolioGap: 0, sp500: 0 },
      { date: "2026-08-04", portfolio: null, portfolioApprox: null, portfolioGap: null, sp500: 0.01 },
      { date: "2026-08-05", portfolio: 0.21, portfolioApprox: null, portfolioGap: 0.21, sp500: 0.03 },
    ];
    renderTooltip(rows[1] as ChartSeriesRow, seriesFor(gapKeysFromRows(rows)));

    expect(screen.queryByText("Portfolio")).toBeNull();
    expect(screen.getByText("S&P 500")).toBeInTheDocument();
    expect(screen.getByText("+1.00%")).toBeInTheDocument();
  });

  it("renders nothing for empty rows", () => {
    const { container } = renderChart([], false);
    expect(container.querySelector(".recharts-responsive-container")).toBeNull();
  });

  it("renders one dashed connector across a genuine data gap (#486)", () => {
    const rows = [
      { date: "2026-09-11", portfolio: 0, portfolioApprox: null, portfolioGap: 0, sp500: 0 },
      { date: "2026-09-12", portfolio: null, portfolioApprox: null, portfolioGap: null, sp500: 0.01 },
      { date: "2026-09-13", portfolio: null, portfolioApprox: null, portfolioGap: null, sp500: 0.012 },
      { date: "2026-09-14", portfolio: 0.1, portfolioApprox: null, portfolioGap: 0.1, sp500: 0.02 },
    ];
    const { container } = renderChart(rows);

    const curves = [...container.querySelectorAll("path.recharts-line-curve")];
    const gapCurve = curves.find((path) => path.getAttribute("stroke-dasharray") === "5 4");
    const solidPortfolio = curves.find(
      (path) => path.getAttribute("stroke-width") === "2.5" && !path.getAttribute("stroke-dasharray"),
    );
    expect(gapCurve).toBeDefined();
    expect(solidPortfolio).toBeDefined();
    expect(gapCurve?.getAttribute("stroke-width")).toBe("2.5");
    // One continuous segment (connectNulls), not two broken pieces.
    expect(gapCurve?.getAttribute("d")?.match(/M/g)?.length).toBe(1);
    // Gap line is underneath the real portfolio stroke.
    expect(curves.indexOf(gapCurve as Element)).toBeLessThan(
      curves.indexOf(solidPortfolio as Element),
    );
    // Interior gap dates have no dots (dot={false} on the gap series).
    expect(container.querySelector(".recharts-dot")).toBeNull();
  });

  it("adds no extra dashed stroke when every date has a real value (#486)", () => {
    const rows = [
      { date: "2026-09-11", portfolio: 0, portfolioApprox: null, portfolioGap: null, sp500: 0 },
      { date: "2026-09-12", portfolio: 0.05, portfolioApprox: null, portfolioGap: null, sp500: 0.01 },
      { date: "2026-09-13", portfolio: 0.08, portfolioApprox: null, portfolioGap: null, sp500: 0.02 },
    ];
    const { container } = renderChart(rows);
    expect(container.querySelector('path[stroke-dasharray]')).toBeNull();
  });

  it("leaves an approximate stretch's dashed stroke as the only extra segment (#493)", () => {
    const rows = [
      { date: "2026-08-03", portfolio: 0, portfolioApprox: null, portfolioGap: 0, sp500: 0 },
      { date: "2026-08-04", portfolio: null, portfolioApprox: 0.1, portfolioGap: 0.1, sp500: 0.02 },
      { date: "2026-08-05", portfolio: null, portfolioApprox: 0.15, portfolioGap: 0.15, sp500: 0.01 },
    ];
    const { container } = renderChart(rows);
    expect(container.querySelectorAll('path[stroke-dasharray="5 4"]').length).toBe(1);
  });

  it("does not draw a dashed connector through real history between two gaps (#486)", () => {
    const rows: ChartSeriesRow[] = [
      { date: "2026-09-11", portfolio: 0, portfolioApprox: null, portfolioGap: 0, sp500: 0 },
      { date: "2026-09-12", portfolio: null, portfolioApprox: null, portfolioGap: null, sp500: 0.01 },
      { date: "2026-09-13", portfolio: null, portfolioApprox: null, portfolioGap: null, sp500: 0.01 },
      { date: "2026-09-14", portfolio: 0.1, portfolioApprox: null, portfolioGap: 0.1, sp500: 0.02 },
      { date: "2026-09-15", portfolio: 0.5, portfolioApprox: null, portfolioGap: null, sp500: 0.03 },
      { date: "2026-09-16", portfolio: 0.1, portfolioApprox: null, portfolioGap: null, sp500: 0.03 },
      { date: "2026-09-17", portfolio: 0.1, portfolioApprox: null, portfolioGap: null, sp500: 0.03 },
      { date: "2026-09-18", portfolio: 0.1, portfolioApprox: null, portfolioGap: null, "portfolioGap:1": 0.1, sp500: 0.03 },
      { date: "2026-09-19", portfolio: null, portfolioApprox: null, portfolioGap: null, sp500: 0.03 },
      { date: "2026-09-20", portfolio: null, portfolioApprox: null, portfolioGap: null, sp500: 0.03 },
      { date: "2026-09-21", portfolio: 0.2, portfolioApprox: null, portfolioGap: null, "portfolioGap:1": 0.2, sp500: 0.04 },
    ];
    const { container } = renderChart(rows);

    const dashed = [...container.querySelectorAll('path[stroke-dasharray="5 4"]')].filter(
      (path) => path.getAttribute("stroke-width") === "2.5",
    );
    expect(dashed).toHaveLength(2);
    for (const path of dashed) {
      expect(path.getAttribute("d")?.match(/M/g)?.length).toBe(1);
    }
  });
});
