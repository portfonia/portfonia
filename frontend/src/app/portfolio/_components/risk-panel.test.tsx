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
import { RiskPanel, betaGaugePosition } from "./risk-panel";

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

const data: PortfolioRiskResponse = {
  base_currency: "USD",
  portfolio_vol: { status: "ok", current: "0.1860", tier: "medium", window_start: "2026-06-01", window_end: "2026-08-24", sample_count: 60, points: [{ date: "2026-08-24", vol: "0.1860" }] },
  benchmark_vol: { code: "sp500", status: "ok", current: "0.1420", tier: "medium", window_start: "2026-06-01", window_end: "2026-08-24", sample_count: 60, points: [{ date: "2026-08-24", vol: "0.1420" }] },
  beta: { status: "ok", value: "1.1200", sample_count: 60 },
  risk: { status: "ok", label: "caution" },
  deviation: { status: "ok", delta: 1 },
  manual_valuation_share: "0.1000",
};

beforeEach(() => getPortfolioRisk.mockReset());

it("clamps the Beta needle while retaining the true number and arc thresholds", () => {
  expect(betaGaugePosition(-0.3)).toEqual({ position: 0, segment: "green" });
  expect(betaGaugePosition(0.99).segment).toBe("green");
  expect(betaGaugePosition(1).segment).toBe("gold");
  expect(betaGaugePosition(2).segment).toBe("red");
  expect(betaGaugePosition(3.4)).toEqual({ position: 100, segment: "red" });
});

it.each(["-0.3000", "3.4000"])("shows the true Beta value %s outside the gauge range", async (value) => {
  getPortfolioRisk.mockResolvedValue({ ...data, beta: { status: "ok", value, sample_count: 60 } });
  render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  expect(await screen.findByText(Number(value).toFixed(2))).toBeInTheDocument();
});

it("shows three cells and keeps Beta on benchmark change", async () => {
  getPortfolioRisk.mockResolvedValueOnce(data).mockResolvedValueOnce({
    ...data, benchmark_vol: { ...data.benchmark_vol, code: "csi300", current: "0.2300" },
  });
  render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  expect(await screen.findByText("1.12")).toBeInTheDocument();
  expect(screen.getByText("18.6%")).toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /Volatility comparison benchmark/i }));
  await user.click(await screen.findByRole("menuitem", { name: "CSI 300" }));
  await waitFor(() => expect(getPortfolioRisk).toHaveBeenLastCalledWith("csi300", "USD"));
  expect(screen.getByText("1.12")).toBeInTheDocument();
  expect(await screen.findByText("23.0%")).toBeInTheDocument();
});

it("shows the questionnaire state only in its affected cells", async () => {
  getPortfolioRisk.mockResolvedValue({ ...data, risk: { status: "no_questionnaire", label: null }, deviation: { status: "no_questionnaire", delta: null } });
  render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  expect(await screen.findByText("1.12")).toBeInTheDocument();
  expect(screen.getAllByText(/No submitted questionnaire/)).toHaveLength(2);
  expect(screen.getByText("18.6%")).toBeInTheDocument();
});

it("reveals the same explanation on hover, focus, and tap", async () => {
  getPortfolioRisk.mockResolvedValue(data);
  render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  const user = userEvent.setup();
  const beta = await screen.findByRole("button", { name: /Beta: 1.12/ });
  await user.hover(beta);
  expect(screen.getByRole("tooltip")).toHaveTextContent(/Historical sensitivity/);
  await user.unhover(beta);
  fireEvent.focus(beta);
  expect(screen.getByRole("tooltip")).toHaveTextContent(/Historical sensitivity/);
  fireEvent.blur(beta);
  await user.click(beta);
  expect(screen.getByRole("tooltip")).toHaveTextContent(/Historical sensitivity/);
});

it("renders independent unavailable states without hiding the chart", async () => {
  getPortfolioRisk.mockResolvedValue({
    ...data,
    beta: { status: "insufficient_sample", value: null, sample_count: 21 },
    risk: { status: "data_quality", label: null },
    deviation: { status: "no_valued_holdings", delta: null },
  });
  render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  expect(await screen.findByText("Insufficient sample")).toBeInTheDocument();
  expect(screen.getByText(/Insufficient data quality/)).toBeInTheDocument();
  expect(screen.getByText(/No valued holdings/)).toBeInTheDocument();
  expect(screen.getByText("18.6%")).toBeInTheDocument();
});

it("shows only the benchmark curve when portfolio samples are insufficient", async () => {
  const insufficient: PortfolioRiskResponse = {
    ...data,
    portfolio_vol: { status: "insufficient_sample", current: null, tier: null, window_start: "2026-06-01", window_end: "2026-08-24", sample_count: 10, points: [] },
    benchmark_vol: { ...data.benchmark_vol, points: [{ date: "2026-08-23", vol: "0.1300" }, { date: "2026-08-24", vol: "0.1420" }] },
    beta: { status: "insufficient_sample", value: null, sample_count: 10 },
    risk: { status: "insufficient_sample", label: null },
    deviation: { status: "ok", delta: 1 },
  };
  getPortfolioRisk.mockResolvedValueOnce(insufficient).mockResolvedValueOnce({
    ...insufficient,
    benchmark_vol: { ...insufficient.benchmark_vol, code: "csi300", current: "0.2300", points: [{ date: "2026-08-23", vol: "0.2100" }, { date: "2026-08-24", vol: "0.2300" }] },
  });
  const { container } = render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  await waitFor(() => expect(container.querySelector('path.recharts-line-curve[stroke="#fbbf24"]')).not.toBeNull());
  expect(container.querySelector('path.recharts-line-curve[stroke="#60a5fa"]')).toBeNull();
  const chart = screen.getByRole("img", { name: /Rolling annualized volatility/ }).closest("section");
  expect(chart).not.toBeNull();
  expect(chart).toHaveTextContent("Portfolio: Insufficient sample");
  expect(chart).toHaveTextContent("S&P 500: 14.2%");
  expect(screen.getByRole("button", { name: /Beta: Insufficient sample/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Risk: Insufficient sample/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Deviation: Aggressive direction \+1/ })).toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /Volatility comparison benchmark/i }));
  await user.click(await screen.findByRole("menuitem", { name: "CSI 300" }));
  await waitFor(() => expect(getPortfolioRisk).toHaveBeenLastCalledWith("csi300", "USD"));
  expect(await screen.findByText("23.0%")).toBeInTheDocument();
  expect(container.querySelector('path.recharts-line-curve[stroke="#60a5fa"]')).toBeNull();
});

it("shows one chart message when neither curve has enough samples", async () => {
  const unavailable = { ...data.portfolio_vol, status: "insufficient_sample" as const, current: null, tier: null, points: [] };
  getPortfolioRisk.mockResolvedValue({
    ...data,
    portfolio_vol: unavailable,
    benchmark_vol: { ...unavailable, code: "sp500" },
    beta: { status: "insufficient_sample", value: null, sample_count: 10 },
    risk: { status: "insufficient_sample", label: null },
  });
  render(<LocaleProvider><RiskPanel baseCurrency="USD" /></LocaleProvider>);
  const chart = (await screen.findByText("Volatility · annualized historical volatility")).closest("section");
  expect(chart?.querySelector('[role="img"]')).toBeNull();
  expect(chart).toHaveTextContent("Insufficient sample");
});
