import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { catalogs } from "@/locales";
import type { Me } from "@/lib/api";
const { summary, submit } = vi.hoisted(() => ({ summary: vi.fn(), submit: vi.fn() }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string, values?: object) => key === "listSeparator" ? (catalogs.en.jade.whatIf as { listSeparator?: string }).listSeparator ?? key : key + (values ? JSON.stringify(values) : "") }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("@/app/_components/locale-provider", () => ({ useLocalizedHref: () => (p: string) => p, useLocale: () => ({ locale: "en" }) }));
vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), getPortfolioSummary: summary, calculateJadeWhatIf: submit }));
vi.mock("../_components/replay-section", () => ({ ReplaySection: () => null }));
vi.mock("../_components/tail-risk-section", () => ({ TailRiskSection: () => null }));
vi.mock("../_components/stress-section", () => ({ StressSection: () => null }));
vi.mock("../_components/style-section", () => ({ StyleSection: () => null }));
vi.mock("../_components/tracking-section", () => ({ TrackingSection: () => null }));
import { WhatIfBody } from "./what-if-body";
import { JadePageBody } from "../_components/jade-page-body";
const me: Me = { email: "a@example.com", credit_balance: "10", delivery_email: null, email_verified_at: "2026-10-01", delivery_email_verified_at: null, tos_accepted_at: null, has_questionnaire: true, has_holdings: true, missing: [], pending_email_verifications: [], report_language: "en", report_currency: "USD", subscription: { status: "active", type: "jade", cadence: "weekly", expires_on: null, cancel_pending: false, next_adjustment_at: null } };
const result = { base_currency: "USD", range: "1Y", columns: ["current", "a", "b"].map(key => ({ key, rule: "none", total: "100000", replay: { status: "ok", range: "1Y", points: [], metrics: { portfolio: null }, coverage: {} }, tail_risk: { status: "ok", levels: [] }, stress: { scenarios: [] } })) };
beforeEach(() => { vi.clearAllMocks(); summary.mockResolvedValue({ holdings: [{ holding_id: "one", name: "One", market_value_base: "403218.77" }, { holding_id: "two", name: "Two", market_value_base: "596532.10" }] }); submit.mockResolvedValue(result); });
afterEach(() => { cleanup(); vi.useRealTimers(); });
async function editor() { render(<WhatIfBody me={me} hadLoadError={false} />); fireEvent.click(await screen.findByRole("button", { name: "columns.a" })); }
it("F1 shares and totals update from edited amounts", async () => {
 await editor(); fireEvent.change(screen.getByRole("spinbutton", { name: "One" }), { target: { value: "500000" } });
 expect(screen.getByTestId("editor-total")).toHaveTextContent("1,096,532.10"); expect(screen.getByTestId("editor-ratio")).toHaveTextContent("109.7%");
 expect(screen.getByTestId("row-one")).toHaveTextContent("45.6%"); expect(screen.getByTestId("row-two")).toHaveTextContent("54.4%");
});
it("F2 displayed band, signed difference, confirmation and cancel", async () => {
 summary.mockResolvedValue({ holdings: [{ holding_id: "one", name: "One", market_value_base: "100000" }] }); await editor();
 for (const [amount, prompt] of [["100000", false], ["100960", false], ["101050", true], ["98900", true]] as const) {
  submit.mockClear(); fireEvent.change(screen.getByRole("spinbutton", { name: "One" }), { target: { value: amount } });
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
 for(const user of [non,me]) { const {container}=render(<JadePageBody me={user} hadLoadError={false} />); expect(container.querySelector('a[href="/jade/what-if"]')!==null).toBe(user===me); cleanup(); }
});
it("F6 ETF list is closed initially with twelve symbols", async () => {
 await editor(); const list=screen.getByTestId("etf-list"); expect(list).not.toHaveAttribute("open"); expect(list.querySelectorAll("li")).toHaveLength(12);
 expect([...list.querySelectorAll("li")].map(e=>e.getAttribute("data-symbol"))).toEqual(["SPY","QQQ","EFA","EEM","2800.HK","510300.SS","ISF.L","EXSA.DE","1306.T","069500.KS","AGG","GLD"]);
});

it("English confirmation separates two out-of-range versions with the locale separator", async () => {
 await editor();
 fireEvent.change(screen.getByRole("spinbutton", { name: "One" }), { target: { value: "500000" } });
 fireEvent.click(screen.getByRole("button", { name: "columns.b" }));
 fireEvent.change(screen.getByRole("spinbutton", { name: "One" }), { target: { value: "500000" } });
 fireEvent.click(screen.getByRole("button", { name: "calculate" }));
 const description = within(await screen.findByRole("alertdialog")).getByText(/^confirmRatios/);
 expect(description.textContent).toContain("; ");
 expect(description.textContent).not.toContain("\uFF1B");
});

it("non-finite exponent amount does not crash the editor", async () => {
 await editor();
 const input = screen.getByRole("spinbutton", { name: "One" });
 // Preserve the number-input string so browser sanitization cannot hide the render failure.
 Object.defineProperty(input, "value", { configurable: true, get: () => "1e309", set: () => {} });
 fireEvent.change(input);
 expect(screen.getByTestId("editor-total")).toHaveTextContent("596,532.10");
 expect(screen.getByRole("spinbutton", { name: "One" })).toBeInTheDocument();
});
