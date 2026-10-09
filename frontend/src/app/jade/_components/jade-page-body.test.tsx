import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
const { refresh, setJadeCadence, getSubscriptionQuote, setSubscription, cancelSubscription } = vi.hoisted(() => ({ refresh: vi.fn(), setJadeCadence: vi.fn(), getSubscriptionQuote: vi.fn(), setSubscription: vi.fn(), cancelSubscription: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));
vi.mock("@/hooks/use-session", () => ({ revalidateSession: vi.fn() }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string, values?: object) => key + (values ? JSON.stringify(values) : "") }));
vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), setJadeCadence, getSubscriptionQuote, setSubscription, cancelSubscription }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("./tail-risk-section", () => ({ TailRiskSection: () => <div data-testid="tail-risk-section" /> }));
vi.mock("./replay-section", () => ({ ReplaySection: () => <div data-testid="replay-section" /> }));
import type { Me } from "@/lib/api";
import { JadePageBody } from "./jade-page-body";
const me: Me = { email: "jade@example.com", credit_balance: "10.00", delivery_email: null, email_verified_at: "2026-10-01T12:00:00Z", delivery_email_verified_at: null, tos_accepted_at: null, has_questionnaire: true, has_holdings: false, missing: [], pending_email_verifications: [], report_language: "en", report_currency: "USD", subscription: { status: "inactive", type: null, cadence: "none", expires_on: null, cancel_pending: false, next_adjustment_at: null } };
const active: Me = { ...me, subscription: { ...me.subscription, status: "active", type: "jade", cadence: "weekly", expires_on: "2026-11-16" } };
beforeEach(() => { vi.clearAllMocks(); });
it("A9 inactive offers subscription without schedule", () => {
  render(<JadePageBody me={me} hadLoadError={false} />);
  expect(screen.getByRole("button", { name: "subscribe" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "subscribe" })).not.toHaveClass("h-auto");
  expect(screen.getByRole("button", { name: "subscribe" })).toHaveClass("h-8");
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
});
it("A9 unverified disables subscription and shows verification note", () => {
  render(<JadePageBody me={{ ...me, email_verified_at: null }} hadLoadError={false} />);
  expect(screen.getByRole("button", { name: "subscribe" })).toBeDisabled();
  expect(screen.getByText("subscriptionVerifyEmail")).toBeInTheDocument();
});
it("A9 active shows cancel and current schedule; cadence writes bypass dialog and daily lock", async () => {
  setJadeCadence.mockResolvedValue({ ...active.subscription, cadence: "mwf" });
  render(<JadePageBody me={{ ...active, subscription: { ...active.subscription, next_adjustment_at: "2099-10-17T00:00:00-04:00" } }} hadLoadError={false} />);
  expect(screen.getByRole("button", { name: "subscriptionCancel" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "subscriptionCancel" })).not.toHaveClass("h-auto");
  expect(screen.getByRole("button", { name: "subscriptionCancel" })).toHaveClass("h-8");
  const select = screen.getByRole("combobox"); expect(select).toHaveValue("weekly");
  fireEvent.change(select, { target: { value: "mwf" } });
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  expect(setJadeCadence).toHaveBeenCalledExactlyOnceWith("mwf");
  expect(getSubscriptionQuote).not.toHaveBeenCalled();
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  expect(screen.getByText("subscriptionNeedsHoldings")).toBeInTheDocument();
});
it("A9 pending cancellation offers resume", () => {
  render(<JadePageBody me={{ ...active, subscription: { ...active.subscription, cancel_pending: true } }} hadLoadError={false} />);
  expect(screen.getByRole("button", { name: "subscriptionResume" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "subscriptionCancel" })).not.toBeInTheDocument();
});
it("A9 cadence failure keeps server value and shows error", async () => {
  setJadeCadence.mockRejectedValue(new Error("unavailable"));
  render(<JadePageBody me={active} hadLoadError={false} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "daily" } });
  expect(await screen.findByRole("alert")).toHaveTextContent("cadenceError");
  expect(screen.getByRole("combobox")).toHaveValue("weekly");
  expect(refresh).not.toHaveBeenCalled();
});
it("Jade subscription quote uses the shared dialog and shows cadence", async () => {
  getSubscriptionQuote.mockResolvedValue({ action: "subscribe", type: "jade", cadence: "daily", fee: "9.99", balance: "10.00", returned: "0.00", balance_after: "0.01", sufficient: true, expires_on: "2026-11-16", first_report_at: "2026-10-16T17:00:00-04:00", needs_holdings: true });
  render(<JadePageBody me={me} hadLoadError={false} />);
  fireEvent.click(screen.getByRole("button", { name: "subscribe" }));
  expect(await screen.findByRole("alertdialog")).toHaveTextContent('subscriptionJadeCadence{"cadence":"reportScheduleOptions.daily"}');
  expect(getSubscriptionQuote).toHaveBeenCalledExactlyOnceWith("jade");
});
it("load failure uses existing Profile error copy", () => {
  render(<JadePageBody me={null} hadLoadError />);
  expect(screen.getByRole("alert")).toHaveTextContent("errorLoadFailed");
});
it("375px component check keeps schedule full width and subscription controls wrapping", () => {
  vi.stubGlobal("innerWidth", 375);
  const { container } = render(<div style={{ width: 375 }}><JadePageBody me={active} hadLoadError={false} /></div>);
  expect(window.innerWidth).toBe(375);
  expect(screen.getByRole("combobox")).toHaveClass("w-full", "min-w-0");
  const cancel = screen.getByRole("button", { name: "subscriptionCancel" });
  expect(cancel).toHaveClass("h-8");
  expect(cancel).not.toHaveClass("h-auto");
  expect(cancel).not.toHaveClass("max-w-full");
  expect(cancel).not.toHaveClass("whitespace-normal");
  expect(cancel.parentElement).toHaveClass("flex-wrap");
  expect(container.querySelector(".min-w-0")).toBeInTheDocument();
  vi.unstubAllGlobals();
});

it.each(["jade", "daily", "weekly"] as const)("A11 replay is Jade-only for %s", (plan) => {
  render(<JadePageBody me={{ ...active, subscription: { ...active.subscription, type: plan } }} hadLoadError={false} />);
  expect(screen.queryByTestId("replay-section") !== null).toBe(plan === "jade");
  expect(screen.queryByTestId("tail-risk-section") !== null).toBe(plan === "jade");
});
