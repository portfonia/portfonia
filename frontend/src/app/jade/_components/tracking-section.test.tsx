import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { catalogs } from "@/locales";
const { getJadeTracking, saveJadeBenchmark } = vi.hoisted(() => ({ getJadeTracking: vi.fn(), saveJadeBenchmark: vi.fn() }));
vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), getJadeTracking, saveJadeBenchmark }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("recharts", () => ({ ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>, LineChart: ({ children }: { children: React.ReactNode }) => <div data-testid="line-chart">{children}</div>, Line: ({ dataKey }: { dataKey: string }) => <span data-testid={`line-${dataKey}`} />, ReferenceLine: ({ y }: { y: number }) => <span data-testid="zero-line" data-y={y} />, XAxis: () => null, YAxis: () => null, Tooltip: () => null, CartesianGrid: () => null }));
import { TrackingSection } from "./tracking-section";
const symbols = ["SPY", "QQQ", "DIA", "ONEQ", "IWF", "IWD", "IWM", "ACWI", "EFA", "EEM", "2800.HK", "510300.SS", "ISF.L", "EXSA.DE", "1306.T", "069500.KS", "AGG", "TLT", "BIL", "GLD", "DBC", "VNQ", "XLE", "CASH"];
const components = [{ symbol: "SPY", weight: 60 }, { symbol: "AGG", weight: 40 }];
const data = { status: "ok", range: "1Y", base_currency: "USD", window_start: "2025-10-07", window_end: "2026-10-07", first_valid_date: "2025-10-07", components, benchmark_saved: false, benchmark_status: "ok", sample_count: 11, horizon_days: 5, min_samples: 10, metrics: { portfolio_return: "0.120000", benchmark_return: "0.035898", excess_return: "0.084102", annualized_excess: "5.331889", tracking_error: "0.104434" }, points: [{ date: "2025-10-07", excess: "0.000000" }, { date: "2026-10-07", excess: "0.084102" }], coverage: { data_quality: false }, proxy_note: false };
type Settings = { currency: "USD" | "EUR" | "CNY"; range: "1Y" | "3Y" | "1M"; benchmark: "sp500" | "csi300" } | null;
function view(settings: Settings = null) {
  const wrap = (s: Settings) => <LocaleProvider routeLocale={null}><div className="dark" style={{ width: 375 }}><TrackingSection settings={s} /></div></LocaleProvider>;
  return { ...render(wrap(settings)), wrap };
}
beforeEach(() => { getJadeTracking.mockReset(); saveJadeBenchmark.mockReset(); });
const save = () => screen.getByRole("button", { name: "Save reference" });
const weights = () => screen.getAllByRole("spinbutton");
const selectors = () => screen.getAllByRole("combobox");
async function ready() { getJadeTracking.mockResolvedValue(data); const result = view(); await screen.findByRole("combobox", { name: /Component 1/ }); return result; }
it("B13 initial request has no settings and editor stays outside inert region", async () => {
  let resolve: (d: typeof data) => void = () => {}; getJadeTracking.mockImplementation(() => new Promise(r => { resolve = r; }));
  const { container } = view(); expect(getJadeTracking).toHaveBeenCalledExactlyOnceWith();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(container.querySelector("[inert]")).toContainElement(screen.getByTestId("tracking-table"));
  await act(async () => resolve(data));
  expect(selectors()[0].closest("[inert]")).toBeNull();
});
it.each(["currency", "range"] as const)("B13 follows %s", async field => {
  getJadeTracking.mockResolvedValue(data);
  const { rerender, wrap } = view({ currency: "USD", range: "1Y", benchmark: "sp500" });
  await screen.findByRole("combobox", { name: /Component 1/ });
  const next: Exclude<Settings, null> = { currency: field === "currency" ? "EUR" : "USD", range: field === "range" ? "3Y" : "1Y", benchmark: "sp500" };
  getJadeTracking.mockResolvedValue({ ...data, base_currency: next.currency, range: next.range });
  rerender(wrap(next)); await waitFor(() => expect(getJadeTracking).toHaveBeenLastCalledWith(next.currency, next.range));
});
it("B13 ignores benchmark-only settings and matching responses", async () => {
  const { rerender, wrap } = await ready();
  rerender(wrap({ currency: "USD", range: "1Y", benchmark: "sp500" }));
  rerender(wrap({ currency: "USD", range: "1Y", benchmark: "csi300" })); await act(async () => {});
  expect(getJadeTracking).toHaveBeenCalledOnce();
});
it("B13 single flight queues only latest settings", async () => {
  const { rerender, wrap } = await ready(); let resolve: (d: typeof data) => void = () => {};
  getJadeTracking.mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockResolvedValueOnce({ ...data, base_currency: "CNY", range: "3Y" });
  rerender(wrap({ currency: "EUR", range: "1Y", benchmark: "sp500" }));
  rerender(wrap({ currency: "EUR", range: "3Y", benchmark: "sp500" })); rerender(wrap({ currency: "CNY", range: "3Y", benchmark: "csi300" }));
  expect(getJadeTracking).toHaveBeenCalledTimes(2);
  await act(async () => resolve({ ...data, base_currency: "EUR" }));
  expect(getJadeTracking).toHaveBeenCalledTimes(3); expect(getJadeTracking).toHaveBeenLastCalledWith("CNY", "3Y");
});
it("B13 failure keeps data and alert without retry", async () => {
  const { rerender, wrap } = await ready(); getJadeTracking.mockRejectedValueOnce(new Error("offline"));
  rerender(wrap({ currency: "EUR", range: "1Y", benchmark: "sp500" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Tracking could not be loaded.");
  expect(screen.getByTestId("tracking-table")).toHaveTextContent("12.00%"); await act(async () => {}); expect(getJadeTracking).toHaveBeenCalledTimes(2);
});
it.each([false, true])("B13 save reloads latest settings with in-flight=%s", async inFlight => {
  const { rerender, wrap } = await ready(); let resolve: (d: typeof data) => void = () => {};
  if (inFlight) { getJadeTracking.mockImplementationOnce(() => new Promise(r => { resolve = r; })); rerender(wrap({ currency: "EUR", range: "1Y", benchmark: "sp500" })); }
  fireEvent.change(weights()[0], { target: { value: "70" } }); fireEvent.change(weights()[1], { target: { value: "30" } });
  const saved = [{ symbol: "SPY", weight: 70 }, { symbol: "AGG", weight: 30 }];
  saveJadeBenchmark.mockResolvedValue({ components: saved, saved: true });
  if (inFlight) rerender(wrap({ currency: "CNY", range: "3Y", benchmark: "csi300" }));
  getJadeTracking.mockResolvedValue({ ...data, components: saved, benchmark_saved: true, base_currency: inFlight ? "CNY" : "USD", range: inFlight ? "3Y" : "1Y" });
  fireEvent.click(save()); await waitFor(() => expect(saveJadeBenchmark).toHaveBeenCalledOnce()); await act(async () => {});
  if (inFlight) { expect(getJadeTracking).toHaveBeenCalledTimes(2); await act(async () => resolve({ ...data, base_currency: "EUR" })); }
  await waitFor(() => expect(getJadeTracking).toHaveBeenCalledTimes(inFlight ? 3 : 2));
  expect(getJadeTracking).toHaveBeenLastCalledWith(...(inFlight ? ["CNY", "3Y"] : []));
});
it("B15 rows picker order disabled used options unchanged save and add/remove bounds", async () => {
  await ready(); expect(weights().map(el => (el as HTMLInputElement).value)).toEqual(["60", "40"]);
  expect(save()).toBeDisabled(); expect([...selectors()[0].querySelectorAll("option")].map(o => o.value)).toEqual(symbols);
  expect(within(selectors()[0]).getByRole("option", { name: /aggregate bonds/ })).toBeDisabled();
  for (let i = 0; i < 3; i++) fireEvent.click(screen.getByRole("button", { name: "Add component" }));
  expect(selectors()).toHaveLength(5); expect(selectors().slice(2).map(el => (el as HTMLSelectElement).value)).toEqual(["QQQ", "DIA", "ONEQ"]);
  expect(weights()[2]).toHaveValue(null); expect(screen.queryByRole("button", { name: "Add component" })).not.toBeInTheDocument();
  for (let i = 0; i < 4; i++) fireEvent.click(screen.getAllByRole("button", { name: "Remove" }).at(-1) as HTMLElement);
  expect(selectors()).toHaveLength(1); expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
});
it.each(["empty", "fraction", "zero", "101", "sum99", "sum101", "duplicate"])("B15 invalid %s disables save", async kind => {
  await ready();
  if (kind === "duplicate") fireEvent.change(selectors()[1], { target: { value: "SPY" } });
  else fireEvent.change(weights()[0], { target: { value: ({ empty: "", fraction: "60.5", zero: "0", "101": "101", sum99: "59", sum101: "61" } as Record<string, string>)[kind] } });
  expect(save()).toBeDisabled(); expect(screen.getByText(/Use whole-number weights/)).toBeInTheDocument();
});
it.each([false, true])("B15 example note saved=%s", async saved => {
  getJadeTracking.mockResolvedValue({ ...data, benchmark_saved: saved }); view(); await screen.findByRole("combobox", { name: /Component 1/ });
  expect(screen.queryByText(/Example reference, not saved/) !== null).toBe(!saved);
});
it("B15 sends ordered rows save failure preserves edits", async () => {
  await ready(); fireEvent.change(weights()[0], { target: { value: "70" } }); fireEvent.change(weights()[1], { target: { value: "30" } });
  saveJadeBenchmark.mockRejectedValue(new Error("422")); fireEvent.click(save());
  expect(await screen.findByRole("alert")).toHaveTextContent("The reference could not be saved.");
  expect(saveJadeBenchmark).toHaveBeenCalledExactlyOnceWith([{ symbol: "SPY", weight: 70 }, { symbol: "AGG", weight: 30 }]);
  expect(weights()[0]).toHaveValue(70); expect(weights()[1]).toHaveValue(30);
});
it("B15 later response preserves edit rows success resets to saved components", async () => {
  const { rerender, wrap } = await ready(); fireEvent.change(weights()[0], { target: { value: "70" } }); fireEvent.change(weights()[1], { target: { value: "30" } });
  rerender(wrap({ currency: "EUR", range: "1Y", benchmark: "sp500" })); await act(async () => {});
  expect(weights()[0]).toHaveValue(70);
  let resolve: (v: object) => void = () => {}; saveJadeBenchmark.mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  fireEvent.click(save()); expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
  fireEvent.change(weights()[0], { target: { value: "80" } });
  await act(async () => resolve({ components: [{ symbol: "SPY", weight: 70 }, { symbol: "AGG", weight: 30 }], saved: true }));
  expect(weights()[0]).toHaveValue(70);
});
it("B16 table one excess line zero line null tracking error and composition cash", async () => {
  getJadeTracking.mockResolvedValue({ ...data, components: [{ symbol: "CASH", weight: 100 }], metrics: { ...data.metrics, tracking_error: null }, sample_count: 9 });
  const { container } = view(); await screen.findByTestId("line-chart");
  const table = screen.getByTestId("tracking-table"); for (const text of ["Your portfolio (cumulative)", "Reference (cumulative)", "Excess return (cumulative)", "Excess return (annualized)", "Tracking error (annualized)", "—"]) expect(table).toHaveTextContent(text);
  expect(screen.getByText("Tracking error needs at least 10 five-day returns; this span has 9.")).toBeInTheDocument();
  expect(container.querySelectorAll('[data-testid^="line-"]')).toHaveLength(2); expect(screen.getByTestId("line-excess")).toBeInTheDocument();
  expect(screen.getByTestId("zero-line")).toHaveAttribute("data-y", "0");
  expect(screen.getByText("Reference: 100% Cash (USD, held constant)")).toBeInTheDocument(); expect(container.querySelector("details")).not.toHaveAttribute("open");
});
it.each(["1M", "3M", "6M", "YTD", "1Y", "3Y", "5Y"])("B16 annualized note for %s", async range => {
  getJadeTracking.mockResolvedValue({ ...data, range }); view(); await screen.findByTestId("line-chart");
  expect(screen.queryByText(/Annualized from this span's data/) !== null).toBe(["1M", "3M", "6M", "YTD"].includes(range));
});
it.each(["ok", "insufficient", "pending", "no_holdings"])("B16 status %s gates flags", async status => {
  getJadeTracking.mockResolvedValue({ ...data, status, metrics: status === "ok" ? data.metrics : null, coverage: { data_quality: true }, proxy_note: true }); view(); await waitFor(() => expect(screen.queryByTestId("calculating-overlay")).not.toBeInTheDocument());
  expect(screen.queryByText(/At least 66%/) !== null).toBe(status === "ok" || status === "insufficient");
  expect(screen.queryByText(/Some holdings follow index ETFs/) !== null).toBe(status === "ok" || status === "insufficient");
});
it.each(["pending", "unavailable"])("B16 reference %s has status notice", async benchmark_status => {
  getJadeTracking.mockResolvedValue({ ...data, benchmark_status, metrics: null, points: [] }); view();
  expect(await screen.findByText(benchmark_status === "pending" ? /Price history for the reference is being prepared/ : /The reference cannot be computed/)).toBeInTheDocument();
  expect(screen.queryByTestId("line-excess")).not.toBeInTheDocument();
});
it("375px editor wraps selects and weight/remove cells table and 240px chart fill card", async () => {
  const { container } = await ready();
  expect(container.firstChild).toHaveStyle({ width: "375px" });
  for (const select of selectors()) { expect(select).toHaveClass("w-full", "min-w-0"); expect(select.closest('[data-tracking-row]')).toHaveClass("flex-col"); }
  for (const input of weights()) expect(input.parentElement?.parentElement).toHaveClass("flex", "min-w-0");
  const table = screen.getByTestId("tracking-table"); expect(table).toHaveClass("grid-cols-2", "min-w-0");
  for (const cell of table.querySelectorAll(".contents > *")) expect(cell).toHaveClass("min-w-0", "break-words");
  expect(screen.getByTestId("tracking-chart")).toHaveClass("h-60", "w-full", "min-w-0");
});
it.each(["en", "zh-Hans", "zh-Hant"] as const)("B17 %s locale parity and D9 labels", locale => {
  const jade = catalogs[locale].jade as unknown as Record<string, { labels: Record<string, string> }>;
  expect(Object.keys(jade.tracking.labels)).toEqual(symbols.map(s => s.replaceAll(".", "_")));
  const keys = (o: object, p = ""): string[] => Object.entries(o).flatMap(([k, v]) => v !== null && typeof v === "object" ? keys(v, `${p}${k}.`) : [`${p}${k}`]).sort();
  expect(keys(jade.tracking)).toEqual(keys((catalogs.en.jade as unknown as typeof jade).tracking));
  for (const [key, value] of Object.entries(jade.style.labels)) expect(jade.tracking.labels[key]).toBe(value);
});
