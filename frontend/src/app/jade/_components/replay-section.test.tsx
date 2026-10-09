import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
import type { JadeReplay } from "@/lib/api";
const { getJadeReplay } = vi.hoisted(() => ({ getJadeReplay: vi.fn() }));
vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), getJadeReplay }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
import { ReplaySection } from "./replay-section";
const metric = { cumulative_return: "0.123456", annualized_return: "0.030000", annualized_vol: "0.180000", max_drawdown: "-0.100000", max_drawdown_peak: "2026-01-01", max_drawdown_trough: "2026-02-01", worst_day: "-0.030000", worst_day_date: "2026-02-01", worst_month: "-0.050000", worst_month_label: "2026-02" };
const data: JadeReplay = {
  status: "ok", base_currency: "USD", benchmark: "sp500", benchmark_symbol: "SPY", benchmark_name: "SPDR S&P 500 ETF Trust", benchmark_status: "ok", window_start: "2021-10-08", window_end: "2026-10-08", first_valid_date: "2021-10-08", sample_count: 1000, skipped_days: 2,
  points: [{ date: "2021-10-08", portfolio: "0.000000", benchmark: "0.000000" }, { date: "2026-10-08", portfolio: "0.123456", benchmark: "0.100000" }],
  metrics: { portfolio: metric, benchmark: metric },
  coverage: { own_share: "0.300000", proxy_share: "0.300000", head_proxy_share: "0.300000", cash_share: "0.050000", cash_assumed_share: "0.050000", approx_share_at_start: "0.650000", data_quality: false, pending_share: null },
  holdings: [{ holding_id: "h1", name: "Recent listing", method: "head_proxy", weight: "0.300000", excluded_reason: null, own_history_unavailable: false, proxy_symbol: "SPY", proxy_name: "SPDR S&P 500 ETF Trust", beta: "2.0000", beta_samples: 59, own_first_date: "2026-06-12", own_vol: "0.857000", proxy_segment_vol: "0.376000" }],
};
beforeEach(() => { getJadeReplay.mockReset(); vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })); });
function view() { return render(<LocaleProvider routeLocale={null}><div style={{ width: 375 }}><ReplaySection /></div></LocaleProvider>); }
it.each([ ["pending", /Price history for your holdings is being prepared/], ["no_holdings", /Add holdings to see a replay/], ["insufficient", /Not enough history to replay yet/], ["ok", /Maximum drawdown/] ] as const)("A11 %s state", async (status, text) => {
  getJadeReplay.mockResolvedValue({ ...data, status, points: status === "ok" ? data.points : [], metrics: { portfolio: status === "ok" ? metric : null, benchmark: null } });
  view(); expect(await screen.findByText(text)).toBeInTheDocument();
  if (status === "insufficient") expect(screen.getByText(/^Recent listing/)).toBeInTheDocument();
});
it("A12 currency refetch disables controls and reverts on failure", async () => {
  let reject!: (error: Error) => void;
  getJadeReplay.mockResolvedValueOnce(data).mockImplementationOnce(() => new Promise((_, r) => { reject = r; }));
  view(); await screen.findByText(/^Recent listing/);
  fireEvent.click(screen.getByRole("button", { name: /USD.*currency/i }));
  fireEvent.click(screen.getByRole("menuitem", { name: "CNY" }));
  expect(getJadeReplay).toHaveBeenLastCalledWith("CNY", "sp500");
  expect(screen.getByRole("button", { name: /CNY.*currency/i })).toBeDisabled();
  expect(screen.getByRole("button", { name: /Benchmark.*S&P/i })).toBeDisabled();
  reject(new Error("offline"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the replay");
  expect(screen.getByRole("button", { name: /USD.*currency/i })).toBeEnabled();
});
it("A12 benchmark refetch keeps currency and uses the response", async () => {
  getJadeReplay.mockResolvedValueOnce(data).mockResolvedValueOnce({ ...data, benchmark: "csi300", benchmark_symbol: "510300.SS" });
  view(); await screen.findByText(/^Recent listing/);
  fireEvent.click(screen.getByRole("button", { name: /Benchmark.*S&P/i }));
  fireEvent.click(screen.getByRole("menuitem", { name: "CSI 300" }));
  await waitFor(() => expect(getJadeReplay).toHaveBeenLastCalledWith("USD", "csi300"));
  expect(await screen.findByText(/CSI 300 \(510300.SS\)/)).toBeInTheDocument();
});
it("A13 disclosure shows data quality, pending share, both volatilities and fallback notes", async () => {
  getJadeReplay.mockResolvedValue({ ...data, coverage: { ...data.coverage, data_quality: true, pending_share: "0.100000" }, holdings: [...data.holdings, { ...data.holdings[0], holding_id: "h2", name: "No own history", method: "proxy", own_history_unavailable: true }] });
  view();
  expect(await screen.findByText(/At least 66%/)).toBeInTheDocument();
  expect(screen.getByText(/10.00% of your holdings/)).toBeInTheDocument();
  expect(screen.getByText(/Volatility: own 85.70%, filled period 37.60%/)).toBeInTheDocument();
  expect(screen.getByText(/beta 2.0000/)).toBeInTheDocument();
  expect(screen.getByText(/beta set to 1/)).toBeInTheDocument();
  expect(screen.getByText(/no price history of its own/)).toBeInTheDocument();
});
it("A13 unavailable benchmark renders dashes and notice", async () => {
  getJadeReplay.mockResolvedValue({ ...data, benchmark_status: "unavailable", metrics: { portfolio: metric, benchmark: null } });
  view(); expect(await screen.findByText("Benchmark data is not available yet.")).toBeInTheDocument();
});
it("375px class check wraps settings, contains chart and metrics, and stacks holdings", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  getJadeReplay.mockResolvedValue(data); const { container } = view(); await screen.findByText(/^Recent listing/);
  expect(screen.getByTestId("replay-settings")).toHaveClass("flex-wrap", "gap-3");
  expect(screen.getByTestId("replay-chart")).toHaveClass("w-full", "min-w-0");
  expect(screen.getByTestId("replay-metrics")).toHaveClass("grid-cols-3", "text-xs", "sm:text-sm");
  expect(screen.getByTestId("replay-holdings")).toHaveClass("flex-col", "break-words");
  expect(container.querySelector("details")).not.toHaveAttribute("open");
  vi.unstubAllGlobals();
});
