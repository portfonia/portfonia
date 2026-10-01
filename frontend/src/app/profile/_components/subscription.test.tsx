import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
const { refresh, getSubscriptionQuote, setSubscription, cancelSubscription } = vi.hoisted(() => ({ refresh: vi.fn(), getSubscriptionQuote: vi.fn(), setSubscription: vi.fn(), cancelSubscription: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string, values?: object) => key + (values ? JSON.stringify(values) : "") }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), getSubscriptionQuote, setSubscription, cancelSubscription }));
vi.mock("./credit-purchase", () => ({ CreditPurchase: () => null }));
import type { Me, Subscription } from "@/lib/api";
import { ProfilePageBody } from "./profile-page-body";
const subscription: Subscription = { status: "inactive", type: null, expires_on: null, cancel_pending: false, next_adjustment_at: null };
const me: Me = { email: "a@example.com", credit_balance: "4.01", delivery_email: null, email_verified_at: "2026-09-30T12:00:00Z", delivery_email_verified_at: null, tos_accepted_at: null, has_questionnaire: true, has_holdings: false, missing: [], pending_email_verifications: [], report_language: "en", report_currency: "USD", subscription };
function show(s: Partial<Subscription> = {}, verified = true) {
  render(<ProfilePageBody me={{ ...me, email_verified_at: verified ? me.email_verified_at : null, subscription: { ...subscription, ...s } }} hadLoadError={false} />);
  return screen.getByRole("combobox", { name: "reportScheduleHeading" });
}
const active = { status: "active", type: "weekly", expires_on: "2026-11-15" } as const;
beforeEach(() => { vi.clearAllMocks(); });
describe("subscription selector and account", () => {
  it.each(["inactive", "expired", "cancelled"] as const)("selector uses non-selectable placeholder for %s", (status) => {
    const select = show({ status, expires_on: "2026-11-15" });
    expect(select).toBeEnabled(); expect(select).toHaveValue("");
    expect(within(select).getByRole("option", { name: "subscriptionNoneOption" })).toBeDisabled();
    expect(within(select).getAllByRole("option")).toHaveLength(3);
    expect(within(select).queryByRole("option", { name: "subscriptionCancel" })).not.toBeInTheDocument();
  });
  it.each([false, true])("active selector preserves server type, cancel pending %s", (cancel_pending) => {
    const select = show({ ...active, cancel_pending });
    expect(select).toHaveValue("weekly"); expect(select).toBeEnabled();
    expect(within(select).getAllByRole("option")).toHaveLength(cancel_pending ? 3 : 4);
    expect(screen.getByText(`subscription${cancel_pending ? "Ends" : "Renews"}{"plan":"reportScheduleOptions.weekly","expires_on":"2026-11-15"}`)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: cancel_pending ? "subscriptionResume" : "subscriptionCancel" })).toBeEnabled();
  });
  it.each([subscription, { ...subscription, ...active }])("unverified selector is disabled and preserves active type", (s) => {
    const select = show(s, false); expect(select).toBeDisabled();
    expect(select).toHaveValue(s.status === "active" ? "weekly" : "");
    expect(screen.getByText("subscriptionVerifyEmail")).toBeInTheDocument();
    expect(within(select).queryByRole("option", { name: "subscriptionCancel" })).not.toBeInTheDocument();
  });
  it.each(["inactive", "cancelled", "expired"] as const)("account highlights %s without a button", (status) => {
    show({ status, expires_on: "2026-11-15" });
    const notice = screen.getByText(status === "expired" ? 'subscriptionExpired{"expires_on":"2026-11-15"}' : "subscriptionNone");
    expect(notice).toHaveAttribute("data-slot", "badge");
    expect(screen.queryByRole("button", { name: "subscriptionCancel" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "subscriptionResume" })).not.toBeInTheDocument();
  });
});

import { ApiError, type SubscriptionQuote } from "@/lib/api";
const quote: SubscriptionQuote = { action: "subscribe", type: "mwf", fee: "1.99", returned: "0.50", balance: "4.01", balance_after: "2.52", sufficient: true, period_start: "2026-10-31", expires_on: "2026-11-30", first_report_at: "2026-11-02T17:00:00-05:00", needs_holdings: false, blocked: null };
async function choose(q: Partial<SubscriptionQuote> = {}) {
  getSubscriptionQuote.mockResolvedValue({ ...quote, ...q });
  const select = show(); fireEvent.change(select, { target: { value: "mwf" } });
  expect(select).toHaveValue("");
  return screen.findByRole("alertdialog");
}
describe("subscription dialogs and writes", () => {
  it.each(["subscribe", "change", "resume"] as const)("renders quote lines for %s in order with holdings warning", async (action) => {
    const dialog = await choose({ action, needs_holdings: true });
    const lines = Array.from(dialog.querySelectorAll("p")).map(p => p.textContent);
    expect(lines).toEqual([
      "subscriptionDescription.mwf",
      ...(action === "resume" ? ['subscriptionContinues{"expires_on":"2026-11-30"}'] : ['subscriptionFee{"fee":"1.99"}', 'subscriptionBalance{"balance":"4.01"}']),
      ...(action === "change" ? ['subscriptionReturned{"returned":"0.50"}', "subscriptionStartsToday"] : []),
      'subscriptionBalanceAfter{"balance_after":"2.52"}', 'subscriptionPaidThrough{"expires_on":"2026-11-30"}',
      'subscriptionFirstReport{"first_report_at":"2026-11-02 17:00 ET"}', "subscriptionNeedsHoldings", "subscriptionDailyRule",
    ]);
    expect(within(dialog).getByRole("heading")).toHaveTextContent("subscriptionTitle.mwf");
    expect(within(dialog).getByRole("button", { name: "subscriptionConfirm" })).toBeEnabled();
    expect(getSubscriptionQuote).toHaveBeenCalledExactlyOnceWith("mwf");
  });
  it("disables insufficient Confirm and shows the top-up key", async () => {
    const dialog = await choose({ sufficient: false });
    expect(within(dialog).getByRole("button", { name: "subscriptionConfirm" })).toBeDisabled();
    expect(within(dialog).getByText("subscriptionTopUp")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "subscriptionConfirm" }));
    expect(setSubscription).not.toHaveBeenCalled();
  });
  it("confirms once, disables while pending, closes and refreshes on success", async () => {
    let resolve: (value: Subscription) => void = () => {};
    setSubscription.mockImplementation(() => new Promise<Subscription>(done => { resolve = done; }));
    const dialog = await choose(); const confirm = within(dialog).getByRole("button", { name: "subscriptionConfirm" });
    fireEvent.click(confirm); expect(confirm).toBeDisabled(); fireEvent.click(confirm);
    expect(setSubscription).toHaveBeenCalledExactlyOnceWith("mwf"); expect(cancelSubscription).not.toHaveBeenCalled();
    resolve(subscription);
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });
  it.each(["daily_limit", "email_unverified", "insufficient_credits", "no_change", "no_subscription"])("shows distinct 409 %s inside plan dialogs", async (code) => {
    setSubscription.mockRejectedValue(new ApiError(409, code));
    const dialog = await choose(); fireEvent.click(within(dialog).getByRole("button", { name: "subscriptionConfirm" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(`subscriptionErrors.${code}`);
    expect(refresh).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "subscriptionDismiss" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });
  it("none quotes open no dialog; failed quotes show the generic key", async () => {
    getSubscriptionQuote.mockResolvedValue({ ...quote, action: "none" });
    const select = show(active); fireEvent.change(select, { target: { value: "mwf" } });
    await waitFor(() => expect(select).toBeEnabled()); expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    getSubscriptionQuote.mockRejectedValue(new Error("provider")); fireEvent.change(select, { target: { value: "mwf" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("subscriptionError");
  });
  it("other write errors show the generic key", async () => {
    setSubscription.mockRejectedValue(new ApiError(500, "internal"));
    const dialog = await choose(); fireEvent.click(within(dialog).getByRole("button", { name: "subscriptionConfirm" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("subscriptionError");
  });
  it.each(["selector", "cancel", "resume"])("daily lock blocks %s and displays 7h 12m", async (entry) => {
    const now = Date.now(); vi.spyOn(Date, "now").mockReturnValue(now);
    const select = show({ ...active, cancel_pending: entry === "resume", next_adjustment_at: new Date(now + (7 * 60 + 12) * 60000).toISOString() });
    if (entry === "selector") fireEvent.change(select, { target: { value: "mwf" } });
    else fireEvent.click(screen.getByRole("button", { name: entry === "resume" ? "subscriptionResume" : "subscriptionCancel" }));
    expect(await screen.findByRole("alert")).toHaveTextContent('subscriptionDailyLock{"hours":7,"minutes":12}');
    expect(getSubscriptionQuote).not.toHaveBeenCalled(); expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument(); vi.restoreAllMocks();
  });
  it.each(["selector", "account"])("cancel from %s shows expiry and calls cancelSubscription", async (entry) => {
    getSubscriptionQuote.mockResolvedValue({ ...quote, action: "none", expires_on: "2026-11-15" });
    cancelSubscription.mockResolvedValue(subscription); const select = show(active);
    if (entry === "selector") fireEvent.change(select, { target: { value: "cancel" } });
    else fireEvent.click(screen.getByRole("button", { name: "subscriptionCancel" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText('subscriptionCancelBody{"expires_on":"2026-11-15"}')).toBeInTheDocument();
    expect(within(dialog).getByText("subscriptionDailyRule")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "subscriptionConfirmCancellation" }));
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
    expect(cancelSubscription).toHaveBeenCalledExactlyOnceWith(); expect(setSubscription).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });
  it.each(["resume", "subscribe"] as const)("Account Resume confirms %s quote using setSubscription current type", async (action) => {
    getSubscriptionQuote.mockResolvedValue({ ...quote, type: "weekly", action }); setSubscription.mockResolvedValue(subscription);
    show({ ...active, cancel_pending: true, expires_on: action === "resume" ? "2026-11-15" : "2026-09-15" });
    fireEvent.click(screen.getByRole("button", { name: "subscriptionResume" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(getSubscriptionQuote).toHaveBeenCalledExactlyOnceWith("weekly");
    expect(within(dialog).queryByText('subscriptionContinues{"expires_on":"2026-11-30"}') !== null).toBe(action === "resume");
    expect(within(dialog).queryByText('subscriptionFee{"fee":"1.99"}') !== null).toBe(action === "subscribe");
    fireEvent.click(within(dialog).getByRole("button", { name: "subscriptionConfirm" }));
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce()); expect(setSubscription).toHaveBeenCalledExactlyOnceWith("weekly"); expect(cancelSubscription).not.toHaveBeenCalled();
  });
});

it.each(["daily_limit", "email_unverified", "insufficient_credits", "no_change", "no_subscription"])("cancel dialog shows 409 %s and re-enables confirmation", async code => {
  getSubscriptionQuote.mockResolvedValue({ ...quote, action: "none", expires_on: "2026-11-15" });
  cancelSubscription.mockRejectedValue(new ApiError(409, code)); show(active);
  fireEvent.click(screen.getByRole("button", { name: "subscriptionCancel" }));
  const dialog = await screen.findByRole("alertdialog");
  const confirm = within(dialog).getByRole("button", { name: "subscriptionConfirmCancellation" });
  fireEvent.click(confirm);
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(`subscriptionErrors.${code}`);
  expect(confirm).toBeEnabled(); expect(refresh).not.toHaveBeenCalled();
  expect(cancelSubscription).toHaveBeenCalledExactlyOnceWith();
});
it("closing a plan dialog performs no write and hides conditional lines", async () => {
  const dialog = await choose();
  expect(within(dialog).queryByText("subscriptionNeedsHoldings")).not.toBeInTheDocument();
  expect(within(dialog).queryByText("subscriptionStartsToday")).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "subscriptionDismiss" }));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  expect(setSubscription).not.toHaveBeenCalled(); expect(cancelSubscription).not.toHaveBeenCalled(); expect(refresh).not.toHaveBeenCalled();
});
