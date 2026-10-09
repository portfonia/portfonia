import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
const { getJadeTailRisk } = vi.hoisted(() => ({ getJadeTailRisk: vi.fn() }));
vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), getJadeTailRisk }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  BarChart: ({ children }: { children: React.ReactNode }) => <div data-testid="bar-chart">{children}</div>,
  Bar: () => null, XAxis: () => null, YAxis: () => null,
  ReferenceLine: ({ x, strokeDasharray }: { x: number; strokeDasharray?: string }) => <span data-testid="reference-line" data-x={x} data-dash={strokeDasharray} />,
}));
const cell = { var: "0.030000", cvar: "0.040000", var_amount: "3000.00", cvar_amount: "4000.00" };
const percentCell = { ...cell, var_amount: null, cvar_amount: null };
const first = { level: 95, available: true, tail_days: 2, tail_windows: 1, daily: cell, monthly: cell, normal_daily: percentCell, normal_monthly: percentCell, benchmark_daily: percentCell, benchmark_monthly: percentCell, reference_daily: "0.025904", reference_monthly: "0.118707" };
const second = { ...first, level: 99, daily: { ...cell, var: "0.050000" } };
const data = { status: "ok", base_currency: "USD", benchmark: "sp500", benchmark_symbol: "SPY", benchmark_name: "SPDR S&P 500 ETF Trust", benchmark_status: "ok", window_start: "2021-10-08", window_end: "2026-10-08", first_valid_date: "2021-10-08", sample_count: 40, month_windows: 20, month_independent: 1, portfolio_value: "100000.00", levels: [first, second], histogram: [{ lower: "-0.050000", upper: "-0.045000", count: 1 }, { lower: "0.000000", upper: "0.005000", count: 39 }], tolerance_status: "ok", coverage: { own_share: "1.000000", proxy_share: "0.000000", head_proxy_share: "0.000000", cash_share: "0.000000", cash_assumed_share: "0.000000", approx_share_at_start: "0.000000", data_quality: false, pending_share: null }, proxy_understates: false };
type Settings = { currency: "USD" | "EUR" | "CNY"; benchmark: "sp500" | "csi300" } | null;
async function view(settings: Settings = null) {
  const { TailRiskSection } = await import("./tail-risk-section");
  const wrap = (s: Settings) => <LocaleProvider routeLocale={null}><div className="dark" style={{ width: 375 }}><TailRiskSection settings={s} /></div></LocaleProvider>;
  return { ...render(wrap(settings)), wrap };
}
beforeEach(() => { getJadeTailRisk.mockReset(); });
it("A14 initial layout is inert under the overlay and settles", async () => {
  let resolve!: (value: typeof data) => void;
  getJadeTailRisk.mockImplementation(() => new Promise(r => { resolve = r; }));
  const { container } = await view();
  expect(getJadeTailRisk).toHaveBeenCalledExactlyOnceWith();
  const overlay = screen.getByTestId("calculating-overlay");
  const inert = container.querySelector("[inert]");
  expect(inert).toContainElement(screen.getByRole("radiogroup"));
  expect(inert).toContainElement(screen.getByTestId("tail-table"));
  expect(screen.getByTestId("tail-table").textContent?.match(/—/g)).toHaveLength(10);
  expect(screen.getByTestId("tail-histogram")).toBeEmptyDOMElement();
  expect(inert).toContainElement(container.querySelector("details"));
  expect(container.querySelector("details")).not.toHaveAttribute("open");
  await act(async () => resolve(data));
  expect(overlay).not.toBeInTheDocument();
});
it("A15 toggle is local, swaps values and reports unavailable 99 sample count", async () => {
  getJadeTailRisk.mockResolvedValue(data);
  const { unmount } = await view(); await screen.findByTestId("bar-chart");
  expect(screen.getByRole("button", { name: "95%" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(screen.getByRole("button", { name: "99%" }));
  expect(within(screen.getByTestId("tail-table")).getByText("-5.00%")).toBeInTheDocument();
  expect(getJadeTailRisk).toHaveBeenCalledOnce(); unmount();
  getJadeTailRisk.mockResolvedValue({ ...data, levels: [first, { ...second, available: false, daily: null, monthly: null, normal_daily: null, normal_monthly: null, benchmark_daily: null, benchmark_monthly: null }] });
  await view(); await screen.findByTestId("bar-chart");
  fireEvent.click(screen.getByRole("button", { name: "99%" }));
  expect(screen.getByText("99% needs at least 500 daily returns; this replay has 40.")).toBeInTheDocument();
  expect(screen.getByTestId("tail-table").textContent?.match(/—/g)).toHaveLength(8);
});
it("A16 table percentages and amounts and questionnaire spanning prompt", async () => {
  getJadeTailRisk.mockResolvedValue({ ...data, tolerance_status: "no_questionnaire", levels: data.levels.map(l => ({ ...l, reference_daily: null, reference_monthly: null })) });
  await view(); await screen.findByTestId("bar-chart");
  const table=screen.getByTestId("tail-table");
  expect(within(table).getAllByText("-3.00%")).toHaveLength(4);
  expect(within(table).getAllByText("-3,000.00 USD")).toHaveLength(2);
  expect(table.querySelectorAll(".contents")[2].textContent).not.toContain("USD");
  const link=screen.getByRole("link",{name:"Go to the questionnaire"});
  expect(link).toHaveAttribute("href","/questionnaire");
  expect(link.parentElement).toHaveClass("col-span-2");
});
it("A17 settings follow, span-equivalent callbacks do not fetch, pending latest wins, failure retains data", async () => {
  let resolve!: (value: typeof data) => void;
  getJadeTailRisk.mockResolvedValueOnce(data).mockImplementationOnce(() => new Promise(r => { resolve=r; })).mockResolvedValueOnce({ ...data, base_currency:"CNY", benchmark:"csi300" }).mockRejectedValueOnce(new Error("offline"));
  const { rerender, wrap } = await view(); await screen.findByTestId("bar-chart");
  rerender(wrap({currency:"USD",benchmark:"sp500"}));
  expect(getJadeTailRisk).toHaveBeenCalledOnce();
  rerender(wrap({currency:"EUR",benchmark:"csi300"}));
  await waitFor(() => expect(getJadeTailRisk).toHaveBeenLastCalledWith("EUR","csi300"));
  rerender(wrap({currency:"CNY",benchmark:"csi300"}));
  rerender(wrap({currency:"CNY",benchmark:"csi300"}));
  expect(getJadeTailRisk).toHaveBeenCalledTimes(2);
  await act(async () => resolve({ ...data,base_currency:"EUR",benchmark:"csi300" }));
  await waitFor(() => expect(getJadeTailRisk).toHaveBeenCalledTimes(3));
  expect(getJadeTailRisk).toHaveBeenLastCalledWith("CNY","csi300");
  rerender(wrap({currency:"EUR",benchmark:"sp500"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Tail risk could not be loaded");
  expect(screen.getByTestId("tail-table")).toHaveTextContent("CNY");
  await act(async () => {}); expect(getJadeTailRisk).toHaveBeenCalledTimes(4);
});
it("A19 histogram uses selected daily lines and reference legend only when present", async () => {
  getJadeTailRisk.mockResolvedValue(data);
  const { unmount } = await view(); await screen.findByTestId("bar-chart");
  expect(screen.getAllByTestId("reference-line").map(e => e.dataset.x)).toEqual(["-0.03","-0.04","-0.025904"]);
  expect(screen.getByText("Reference line (1 day)")).toBeInTheDocument(); unmount();
  getJadeTailRisk.mockResolvedValue({ ...data,levels:data.levels.map(l => ({...l,reference_daily:null,reference_monthly:null})) });
  await view(); await screen.findByTestId("bar-chart");
  expect(screen.queryByText("Reference line (1 day)")).not.toBeInTheDocument();
});
it("A20 notices follow flags, desktop model stays closed and unavailable normal line is absent", async () => {
  vi.stubGlobal("innerWidth",1024);
  getJadeTailRisk.mockResolvedValue({ ...data,proxy_understates:true,coverage:{...data.coverage,data_quality:true},levels:[first,{...second,available:false,normal_daily:null,normal_monthly:null,daily:null,monthly:null}] });
  const {container,unmount}=await view(); await screen.findByTestId("bar-chart");
  expect(container.querySelector("details")).not.toHaveAttribute("open");
  expect(screen.getByText(/At least 66%/)).toBeInTheDocument();
  expect(screen.getByText(/may understate tail losses/)).toBeInTheDocument();
  expect(screen.getByText(/^If returns followed/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"99%"}));
  expect(screen.queryByText(/^If returns followed/)).not.toBeInTheDocument();
  unmount();getJadeTailRisk.mockResolvedValue(data);await view();await screen.findByTestId("bar-chart");
  expect(screen.queryByText(/At least 66%/)).not.toBeInTheDocument();
  expect(screen.queryByText(/may understate tail losses/)).not.toBeInTheDocument();
  vi.unstubAllGlobals();
});
it("375px dark component classes wrap the table toggle and histogram", async () => {
  getJadeTailRisk.mockResolvedValue(data);
  await view();await screen.findByTestId("bar-chart");
  expect(screen.getByRole("radiogroup")).toHaveClass("flex-wrap");
  const table=screen.getByTestId("tail-table");expect(table).toHaveClass("grid-cols-3","text-xs","sm:text-sm");
  for (const cell of table.querySelectorAll(".contents > *")) expect(cell).toHaveClass("min-w-0","break-words");
  expect(screen.getByTestId("tail-histogram")).toHaveClass("h-48","w-full","min-w-0");
});
