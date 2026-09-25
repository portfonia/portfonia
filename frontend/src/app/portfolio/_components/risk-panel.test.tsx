import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

const { getPortfolioRisk } = vi.hoisted(() => ({ getPortfolioRisk: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, getPortfolioRisk };
});
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { PortfolioRiskResponse } from "@/lib/api";
import { RiskChartTooltip, RiskPanel, betaSegment, toTimestamp } from "./risk-panel";

// Recharts needs a reported size before it emits actual SVG paths.
class SizedResizeObserver {
  private callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) { this.callback = callback; }
  observe(target: Element): void {
    this.callback([{
      target,
      contentRect: {
        x: 0, y: 0, width: 800, height: 300, top: 0, right: 800, bottom: 300,
        left: 0, toJSON: () => ({}),
      },
    } as ResizeObserverEntry], this as unknown as ResizeObserver);
  }
  unobserve(): void {}
  disconnect(): void {}
}
if (typeof globalThis.ResizeObserver !== "undefined") {
  (globalThis as { ResizeObserver: unknown }).ResizeObserver = SizedResizeObserver;
}

const benchmark = { code: "sp500" as const, status: "ok" as const, current: "0.1420", tier: "medium" as const, window_start: "2026-06-01", window_end: "2026-08-24", sample_count: 60, points: [{ date: "2026-08-23", vol: "0.1300" }, { date: "2026-08-24", vol: "0.1420" }] };
const data: PortfolioRiskResponse = {
  base_currency: "USD",
  portfolio_vol: { status: "ok", current: "0.1860", tier: "medium", window_start: "2026-06-01", window_end: "2026-08-24", sample_count: 60, points: [{ date: "2026-08-23", vol: "0.1800" }, { date: "2026-08-24", vol: "0.1860" }] },
  benchmark_vols: [benchmark],
  beta: { status: "ok", value: "1.1200", sample_count: 60 },
  risk: { status: "ok", label: "caution" },
  deviation: { status: "ok", delta: 1 },
  manual_valuation_share: "0.1000",
};
const csi = { ...benchmark, code: "csi300" as const, current: "0.2300", points: [{ date: "2026-08-23", vol: "0.2200" }, { date: "2026-08-24", vol: "0.2300" }] };

beforeEach(() => getPortfolioRisk.mockReset());

it.each([
  [-1, 0, Math.PI], [-0.2, 1, Math.PI * 0.8], [0.6, 2, Math.PI * 0.6],
  [1.12, 2, Math.PI * 0.47], [1.4, 3, Math.PI * 0.4], [2.2, 4, Math.PI * 0.2],
  [3, 4, 0], [3.4, 4, 0], [-1.4, 0, Math.PI],
])("maps beta %s to segment %s and clamped angle", (value, index, angle) => {
  expect(betaSegment(value).index).toBe(index);
  expect(betaSegment(value).angle).toBeCloseTo(angle);
});

it.each(["-1.4000", "3.4000", null])("renders five rounded arcs and semantic triangle markers for %s", async (value) => {
  getPortfolioRisk.mockResolvedValue({ ...data, beta: { status: value === null ? "insufficient_sample" : "ok", value, sample_count: 60 } });
  const { container } = render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  const cell = await screen.findByRole("button", { name: /Beta:/ });
  expect(cell.querySelectorAll("svg path")).toHaveLength(5);
  expect(Array.from(cell.querySelectorAll("svg path"), (path) => path.getAttribute("stroke-linecap"))).toEqual(Array(5).fill("round"));
  expect(cell.querySelector('[data-testid="beta-reference-marker"]')).not.toBeNull();
  if (value === null) expect(cell.querySelector('[data-testid="beta-portfolio-marker"]')).toBeNull();
  else expect(cell.querySelector('[data-testid="beta-portfolio-marker"]')).not.toBeNull();
  expect(cell.querySelector("svg line")).toBeNull();
  expect(cell).toHaveTextContent(value === null ? "Insufficient sample" : Number(value).toFixed(2));
  expect(cell).toHaveTextContent("β=−1");
  expect(container).toHaveTextContent("β=3");
});

it.each(["within", "caution", "exceeds"] as const)("places Risk marker over %s and Deviation over delta+2", async (label) => {
  getPortfolioRisk.mockResolvedValue({ ...data, risk: { status: "ok", label }, deviation: { status: "ok", delta: -1 } });
  render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  const risk = await screen.findByRole("button", { name: /^Risk:/ });
  const deviation = screen.getByRole("button", { name: /^Deviation:/ });
  expect(risk.querySelectorAll('[data-testid="bar-segment"]')).toHaveLength(3);
  expect(deviation.querySelectorAll('[data-testid="bar-segment"]')).toHaveLength(5);
  expect(Array.from(risk.querySelectorAll('[data-testid="bar-segment"]')).findIndex((node) => node.querySelector('[data-testid="bar-marker"]'))).toBe(({ within: 0, caution: 1, exceeds: 2 })[label]);
  expect(Array.from(deviation.querySelectorAll('[data-testid="bar-segment"]')).findIndex((node) => node.querySelector('[data-testid="bar-marker"]'))).toBe(1);
});

it("omits bar markers with no conclusion and keeps explanation behavior", async () => {
  getPortfolioRisk.mockResolvedValue({ ...data, risk: { status: "no_questionnaire", label: null }, deviation: { status: "no_questionnaire", delta: null } });
  render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  const risk = await screen.findByRole("button", { name: /^Risk:/ });
  expect(risk.querySelector('[data-testid="bar-marker"]')).toBeNull();
  expect(screen.getByRole("button", { name: /^Deviation:/ }).querySelector('[data-testid="bar-marker"]')).toBeNull();
  fireEvent.focus(risk);
  expect(screen.getByRole("tooltip")).toHaveTextContent("No submitted investment questionnaire.");
  fireEvent.blur(risk);
});

it("requests multiple benchmarks, uses shared colours and widths, and permits none", async () => {
  getPortfolioRisk.mockResolvedValueOnce(data).mockResolvedValueOnce({ ...data, benchmark_vols: [benchmark, csi] }).mockResolvedValueOnce({ ...data, benchmark_vols: [csi] }).mockResolvedValueOnce({ ...data, benchmark_vols: [] });
  const { container } = render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  await waitFor(() => expect(container.querySelectorAll("path.recharts-line-curve")).toHaveLength(2));
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /Volatility comparison benchmark/i }));
  await user.click(await screen.findByRole("menuitemcheckbox", { name: "CSI 300" }));
  await waitFor(() => expect(getPortfolioRisk).toHaveBeenLastCalledWith(["sp500", "csi300"], "USD"));
  await waitFor(() => expect(container.querySelectorAll("path.recharts-line-curve")).toHaveLength(3));
  expect(container.querySelector('path.recharts-line-curve[stroke="var(--chart-1)"]')).toHaveAttribute("stroke-width", "2.5");
  expect(container.querySelector('path.recharts-line-curve[stroke="var(--chart-2)"]')).toHaveAttribute("stroke-width", "1.5");
  expect(container.querySelector('path.recharts-line-curve[stroke="var(--chart-csi300)"]')).toHaveAttribute("stroke-width", "1.5");
  await user.click(await screen.findByRole("menuitemcheckbox", { name: "S&P 500" }));
  await waitFor(() => expect(getPortfolioRisk).toHaveBeenLastCalledWith(["csi300"], "USD"));
  await user.click(await screen.findByRole("menuitemcheckbox", { name: "CSI 300" }));
  await waitFor(() => expect(getPortfolioRisk).toHaveBeenLastCalledWith([], "USD"));
  await waitFor(() => expect(container.querySelectorAll("path.recharts-line-curve")).toHaveLength(1));
});

it("disables the selector and hides stale results during refetch", async () => {
  let finish!: (value: PortfolioRiskResponse) => void;
  getPortfolioRisk.mockResolvedValueOnce(data).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  await screen.findByText("18.6%");
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /Volatility comparison benchmark/i }));
  await user.click(await screen.findByRole("menuitemcheckbox", { name: "CSI 300" }));
  await waitFor(() => expect(screen.getByRole("button", { name: /Volatility comparison benchmark/i })).toBeDisabled());
  expect(screen.queryByText("18.6%")).toBeNull();
  finish({ ...data, benchmark_vols: [benchmark, csi] });
  await screen.findByText("23.0%");
});

it("uses compact Performance axes with no more than six x ticks", async () => {
  const points = Array.from({ length: 12 }, (_, i) => ({ date: `2026-08-${String(i + 10).padStart(2, "0")}`, vol: "0.1860" }));
  getPortfolioRisk.mockResolvedValue({ ...data, portfolio_vol: { ...data.portfolio_vol, points }, benchmark_vols: [] });
  const { container } = render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  await waitFor(() => expect(container.querySelector(".recharts-xAxis")).not.toBeNull());
  expect(container.querySelectorAll(".recharts-xAxis .recharts-cartesian-axis-tick").length).toBeLessThanOrEqual(6);
  for (const axis of container.querySelectorAll(".recharts-cartesian-axis")) {
    expect(axis.querySelector(".recharts-cartesian-axis-line")).toBeNull();
    expect(axis.querySelector(".recharts-cartesian-axis-tick-line")).toBeNull();
    for (const tick of axis.querySelectorAll(".recharts-cartesian-axis-tick-value")) {
      expect(tick).toHaveAttribute("fill", "var(--muted-foreground)");
      expect(tick).toHaveAttribute("font-size", "12");
    }
  }
});

it("lists every drawn series with as-of dates", () => {
  const series = [
    { name: "Portfolio", color: "var(--chart-1)", points: [{ t: toTimestamp("2026-08-24"), vol: 0.186 }] },
    { name: "S&P 500", color: "var(--chart-2)", points: [{ t: toTimestamp("2026-08-25"), vol: 0.142 }] },
    { name: "CSI 300", color: "var(--chart-csi300)", points: [{ t: toTimestamp("2026-08-23"), vol: 0.23 }] },
  ];
  render(<LocaleProvider><RiskChartTooltip active label={toTimestamp("2026-08-25")} series={series} /></LocaleProvider>);
  const tooltip = screen.getByRole("tooltip");
  expect(tooltip).toHaveTextContent("Portfolio");
  expect(tooltip).toHaveTextContent("S&P 500");
  expect(tooltip).toHaveTextContent("CSI 300");
  expect(tooltip).toHaveTextContent("as of Aug 24, 2026");
  expect(tooltip).toHaveTextContent("as of Aug 23, 2026");
});

it("draws every available benchmark without portfolio data, or one empty message", async () => {
  const empty = { ...data.portfolio_vol, status: "insufficient_sample" as const, current: null, tier: null, points: [] };
  getPortfolioRisk.mockResolvedValueOnce({ ...data, portfolio_vol: empty, benchmark_vols: [benchmark, csi] }).mockResolvedValueOnce({ ...data, portfolio_vol: empty, benchmark_vols: [{ ...benchmark, status: "insufficient_sample", current: null, points: [] }] });
  const { container, rerender } = render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  await waitFor(() => expect(container.querySelectorAll("path.recharts-line-curve")).toHaveLength(2));
  expect(screen.getByRole("button", { name: /Beta:/ })).toBeInTheDocument();
  rerender(<LocaleProvider><RiskPanel baseCurrency="CNY" /></LocaleProvider>);
  await waitFor(() => expect(container.querySelector('[role="img"]')).toBeNull());
  expect(screen.getByText("Volatility · annualized historical volatility").closest("section")).toHaveTextContent("Insufficient sample");
});
