import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { catalogs } from "@/locales";
import type { Me } from "@/lib/api";
const { summary, submit } = vi.hoisted(() => ({ summary: vi.fn(), submit: vi.fn() }));
vi.mock("next-intl", () => ({ useTranslations: (namespace?: string) => (key: string, values?: object) => !namespace ? key.replace(/^jade\.whatIf\./, "").replace(/^jade\.replay\./, "") : key === "listSeparator" ? (catalogs.en.jade.whatIf as { listSeparator?: string }).listSeparator ?? key : key + (values ? JSON.stringify(values) : "") }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("@/app/_components/locale-provider", () => ({ useLocalizedHref: () => (p: string) => p, useLocale: () => ({ locale: "en" }) }));
vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), getPortfolioSummary: summary, calculateJadeWhatIf: submit }));
vi.mock("../_components/replay-section", () => ({ ReplaySection: () => null }));
vi.mock("../_components/tail-risk-section", () => ({ TailRiskSection: () => null }));
vi.mock("../_components/stress-section", () => ({ StressSection: () => null }));
vi.mock("../_components/style-section", () => ({ StyleSection: () => null }));
vi.mock("../_components/tracking-section", () => ({ TrackingSection: () => null }));
import { WhatIfBody, normalizeAmount } from "./what-if-body";
import { JadePageBody } from "../_components/jade-page-body";
const me: Me = { email: "a@example.com", credit_balance: "10", delivery_email: null, email_verified_at: "2026-10-01", delivery_email_verified_at: null, tos_accepted_at: null, has_questionnaire: true, has_holdings: true, missing: [], pending_email_verifications: [], report_language: "en", report_currency: "USD", subscription: { status: "active", type: "jade", cadence: "weekly", expires_on: null, cancel_pending: false, next_adjustment_at: null } };
const result = { base_currency: "USD", range: "1Y", columns: ["current", "a", "b"].map(key => ({ key, rule: "none", total: "100000", replay: { status: "ok", range: "1Y", points: [], metrics: { portfolio: null }, coverage: {} }, tail_risk: { status: "ok", levels: [] }, stress: { scenarios: [] } })) };
beforeEach(() => { vi.clearAllMocks(); summary.mockResolvedValue({ holdings: [{ holding_id: "one", name: "One", market_value_base: "403218.77" }, { holding_id: "two", name: "Two", market_value_base: "596532.10" }] }); submit.mockResolvedValue(result); });
afterEach(() => { cleanup(); vi.useRealTimers(); });
async function editor() { render(<WhatIfBody me={me} hadLoadError={false} />); fireEvent.click(await screen.findByRole("button", { name: "columns.a" })); }
it("F1 shares and totals update from edited amounts", async () => {
 await editor(); fireEvent.change(screen.getByRole("textbox", { name: "One" }), { target: { value: "500000" } });
 expect(screen.getByTestId("editor-total")).toHaveTextContent("1,096,532.10"); expect(screen.getByTestId("editor-ratio")).toHaveTextContent("109.7%");
 expect(screen.getByTestId("row-one")).toHaveTextContent("45.6%"); expect(screen.getByTestId("row-two")).toHaveTextContent("54.4%");
});
it("F2 displayed band, signed difference, confirmation and cancel", async () => {
 summary.mockResolvedValue({ holdings: [{ holding_id: "one", name: "One", market_value_base: "100000" }] }); await editor();
 for (const [amount, prompt] of [["100000", false], ["100960", false], ["101050", true], ["98900", true]] as const) {
  submit.mockClear(); fireEvent.change(screen.getByRole("textbox", { name: "One" }), { target: { value: amount } });
  expect(screen.getByTestId("editor-difference")).toHaveTextContent(new Intl.NumberFormat("en", { style:"currency", currency:"USD", minimumFractionDigits:2, signDisplay:"always" }).format(Number(amount)-100000));
  fireEvent.click(screen.getByRole("button", { name: "calculate" }));
  if (prompt) { const dialog=await screen.findByRole("alertdialog"); fireEvent.click(within(dialog).getByRole("button", { name:"cancel" })); expect(submit).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole("button", { name:"calculate" })); fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name:"confirm" })); }
  await waitFor(() => expect(submit).toHaveBeenCalledOnce()); await waitFor(() => expect(screen.getByRole("button", {name:"calculate"})).toBeEnabled());
 }
});
it("F3 cooldown message and countdown disable Calculate until expiry", async () => {
 await editor(); const { WhatIfCooldownError } = await import("@/lib/api"); submit.mockRejectedValue(new WhatIfCooldownError(2));
 vi.useFakeTimers();
 await act(async () => { fireEvent.click(screen.getByRole("button", {name:"calculate"})); });
 expect(screen.getByRole("button", {name:"calculate"})).toBeDisabled();
 expect(screen.getByTestId("cooldown")).toHaveTextContent("cooldown");
 await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
 await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
 expect(screen.getByRole("button", {name:"calculate"})).toBeEnabled();
});
it("F4 watermark is a repeated diagonal layer inside results", async () => {
 await editor(); fireEvent.click(screen.getByRole("button", { name:"calculate" })); const results=await screen.findByTestId("what-if-results");
 const watermark=within(results).getByTestId("watermark"); expect(watermark).toHaveAttribute("aria-hidden","true"); expect(watermark).toHaveClass("pointer-events-none","select-none"); expect(watermark.children.length).toBeGreaterThan(1);
});
it("F5 access notice links to Jade and CTA appears only for Jade", () => {
 const non={...me, subscription:{...me.subscription,type:"daily" as const}};
 render(<WhatIfBody me={non} hadLoadError={false} />); expect(screen.getByRole("link")).toHaveAttribute("href","/jade"); cleanup();
 for(const user of [non,me]) { const {container}=render(<JadePageBody guest={false} demo={null} me={user} hadLoadError={false} />); expect(container.querySelector('a[href="/jade/what-if"]')!==null).toBe(user===me); cleanup(); }
});
it("F6 ETF list is closed initially with twelve symbols", async () => {
 await editor(); const list=screen.getByTestId("etf-list"); expect(list).not.toHaveAttribute("open"); expect(list.querySelectorAll("li")).toHaveLength(12);
 expect([...list.querySelectorAll("li")].map(e=>e.getAttribute("data-symbol"))).toEqual(["SPY","QQQ","EFA","EEM","2800.HK","510300.SS","ISF.L","EXSA.DE","1306.T","069500.KS","AGG","GLD"]);
});

it("English confirmation separates two out-of-range versions with the locale separator", async () => {
 await editor();
 fireEvent.change(screen.getByRole("textbox", { name: "One" }), { target: { value: "500000" } });
 fireEvent.click(screen.getByRole("button", { name: "columns.b" }));
 fireEvent.change(screen.getByRole("textbox", { name: "One" }), { target: { value: "500000" } });
 fireEvent.click(screen.getByRole("button", { name: "calculate" }));
 const description = within(await screen.findByRole("alertdialog")).getByText(/^confirmRatios/);
 expect(description.textContent).toContain("; ");
 expect(description.textContent).not.toContain("\uFF1B");
});

it("non-finite exponent amount does not crash the editor", async () => {
 await editor();
 const input = screen.getByRole("textbox", { name: "One" });
 // Preserve the exponent string to check the editor does not throw.
 Object.defineProperty(input, "value", { configurable: true, get: () => "1e309", set: () => {} });
 fireEvent.change(input);
 expect(screen.getByTestId("editor-total")).toHaveTextContent("596,532.10");
 expect(screen.getByRole("textbox", { name: "One" })).toBeInTheDocument();
});

async function calculateEdited() {
 fireEvent.click(screen.getByRole("button", { name: "calculate" }));
 const dialog = screen.queryByRole("alertdialog");
 if (dialog) fireEvent.click(within(dialog).getByRole("button", { name: "confirm" }));
 await waitFor(() => expect(submit).toHaveBeenCalledOnce());
}
it("801 A1 normalizes the specified amount examples", () => {
 for (const [raw, expected] of [["", "0"], ["  ", "0"], ["1,234.567", "1234.56"], ["1\uFF0C000", "1000"], [".5", "0.5"], ["12.", "12"], ["0.999", "0.99"], ["1.2.3", null], ["-5", null], ["1e5", null], ["abc", null]]) expect(normalizeAmount(raw!)).toBe(expected);
});
it("801 A2 clears and blurs to zero and submits zero", async () => {
 await editor(); const input = screen.getByLabelText("One");
 fireEvent.change(input, { target: { value: "" } }); fireEvent.blur(input);
 expect(input).toHaveValue("0"); await calculateEdited();
 expect(submit.mock.calls[0][0].versions[0].rows[0].amount).toBe("0");
});
it("801 A3 submits both comma forms without separators", async () => {
 await editor();
 fireEvent.change(screen.getByLabelText("One"), { target: { value: "1,000" } });
 fireEvent.change(screen.getByLabelText("Two"), { target: { value: "1\uFF0C000" } });
 await calculateEdited();
 expect(submit.mock.calls[0][0].versions[0].rows.map((row: { amount: string }) => row.amount)).toEqual(["1000", "1000"]);
});
it("801 A4 truncates submitted fractional digits", async () => {
 await editor(); fireEvent.change(screen.getByLabelText("One"), { target: { value: "12.345" } });
 await calculateEdited(); expect(submit.mock.calls[0][0].versions[0].rows[0].amount).toBe("12.34");
});
it("801 A5 invalid amount marks input and blocks calculation", async () => {
 await editor(); const input = screen.getByLabelText("One");
 fireEvent.change(input, { target: { value: "1.2.3" } });
 expect(input).toHaveAttribute("aria-invalid", "true"); expect(input).toHaveClass("border-destructive");
 expect(screen.getByRole("button", { name: "calculate" })).toBeDisabled();
 expect(screen.getByRole("alert")).toHaveTextContent("errors.invalidAmount");
});
it("801 A6 maps API errors to the corresponding catalog keys", async () => {
 const { ApiError } = await import("@/lib/api");
 for (const [status, message, key] of [[422, "invalid_amount", "errors.invalidAmount"], [422, "empty_version", "errors.emptyVersion"], [422, "holding_not_found", "errors.stale"], [500, "failure", "loadError"]] as const) {
  await editor(); submit.mockRejectedValueOnce(new ApiError(status, message));
  fireEvent.click(screen.getByRole("button", { name: "calculate" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(key); cleanup();
 }
});

it("804 A7 keeps the slow calculation notice visible while the request is pending", async () => {
 let finish!: (value: typeof result) => void;
 submit.mockReturnValue(new Promise<typeof result>(resolve => { finish = resolve; }));
 await editor();
 fireEvent.click(screen.getByRole("button", { name: "calculate" }));
 const overlay = await screen.findByTestId("calculating-overlay");
 // This file's translation mock returns the common catalog key.
 expect(overlay).toHaveTextContent("calculatingSlow");
 await act(async () => { finish(result); });
 expect(screen.queryByTestId("calculating-overlay")).not.toBeInTheDocument();
});

const { axis806, tooltip806, yAxis806 } = vi.hoisted(() => ({ axis806: vi.fn(), tooltip806: vi.fn(), yAxis806: vi.fn() }));
vi.mock("recharts", async () => {
 const actual = await vi.importActual<typeof import("recharts")>("recharts");
 const Container = ({ children }: { children: import("react").ReactNode }) => <div>{children}</div>;
 return { ...actual, ResponsiveContainer: Container, LineChart: Container,
  XAxis: (props: object) => { axis806(props); return null; },
  YAxis: (props: object) => { yAxis806(props); return null; },
  Tooltip: (props: object) => { tooltip806(props); return null; } };
});
import { WhatIfResults } from "./what-if-results";
import { WhatIfGuide } from "@/app/guides/_components/what-if-guide";
import { whatIfExample } from "@/app/guides/_lib/example-what-if";
import { formatFullDate } from "@/app/portfolio/performance/_components/performance-format";
const fiveYear806 = { ...result, range: "5Y", columns: result.columns.map(column => ({ ...column, replay: { ...column.replay, points: [
 { date: "2021-01-24", portfolio: "0", benchmark: null },
 { date: "2026-01-24", portfolio: "0.5", benchmark: null },
] } })) } as unknown as import("@/lib/api").JadeWhatIf;
it("806 A2 what-if chart dates include the year for a five-year fixture", () => {
 axis806.mockClear(); tooltip806.mockClear();
 render(<WhatIfResults data={fiveYear806} />);
 const time = Date.parse("2023-01-24");
 expect(axis806.mock.lastCall?.[0].tickFormatter(time)).toContain("2023");
 expect(tooltip806.mock.lastCall?.[0].labelFormatter(time)).toContain("2023");
});
it("806 A4 both what-if charts reserve 64 pixels for percentage labels", () => {
 yAxis806.mockClear(); render(<WhatIfResults data={fiveYear806} />);
 expect(yAxis806).toHaveBeenCalledWith(expect.objectContaining({ width: 64 }));
 cleanup(); yAxis806.mockClear(); render(<WhatIfGuide example={whatIfExample()} figureOnly />);
 expect(yAxis806).toHaveBeenCalledWith(expect.objectContaining({ width: 64 }));
});
it("806 A5 both what-if chart containers use the platform tooltip text size", () => {
 const results = render(<WhatIfResults data={fiveYear806} />);
 expect(results.container.querySelector(".h-64")).toHaveClass("text-sm");
 cleanup(); const guide = render(<WhatIfGuide example={whatIfExample()} figureOnly />);
 expect(guide.container.querySelector("[data-guide-chart]")).toHaveClass("text-sm");
});
it("806 A6 calculation basis starts closed and includes the replay date window", () => {
 const { container } = render(<WhatIfResults data={fiveYear806} />);
 const details = container.querySelector("details");
 expect(details).not.toBeNull();
 expect(details).not.toHaveAttribute("open");
 expect(details?.querySelector("summary")).toHaveTextContent("basis.summary");
 expect(details).toHaveTextContent("basis.window" + JSON.stringify({ start: formatFullDate("2021-01-24", "en"), end: formatFullDate("2026-01-24", "en") }));
});
it("806 A7 every column header labels its total", () => {
 render(<WhatIfResults data={fiveYear806} />);
 for (const column of fiveYear806.columns) {
  const name = `columns.${column.key}`;
  const header = screen.getAllByText((_, element) => element?.tagName === "P" && element.textContent?.startsWith(`${name} ·`) === true);
  expect(header).toHaveLength(1);
  expect(header[0]).toHaveTextContent("total");
 }
});
