import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
const { getSubscriptionQuote } = vi.hoisted(() => ({ getSubscriptionQuote: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("./credit-purchase", () => ({ CreditPurchase: () => null }));
vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), getSubscriptionQuote }));
import { LocaleProvider } from "@/app/_components/locale-provider";
import type { Me } from "@/lib/api";
import { ProfilePageBody } from "./profile-page-body";
const me: Me = { email: "daily@example.com", credit_balance: "5.00", delivery_email: null, email_verified_at: "2026-10-01T12:00:00Z", delivery_email_verified_at: null, tos_accepted_at: null, has_questionnaire: true, has_holdings: true, missing: [], pending_email_verifications: [], report_language: "en", report_currency: "USD", subscription: { status: "active", type: "daily", expires_on: "2026-11-01", cancel_pending: false, next_adjustment_at: null } };
beforeEach(() => { vi.clearAllMocks(); });
it("daily_acceptance_12 Profile offers Daily and labels the Daily subscriber", () => {
  render(<LocaleProvider><ProfilePageBody me={me} hadLoadError={false} /></LocaleProvider>);
  const option = screen.getByRole("option", { name: "Daily (Advanced)" });
  expect(option).toHaveValue("daily");
  expect(screen.getByRole("combobox", { name: "Report schedule" })).toHaveValue("daily");
  expect(screen.getByText("Daily briefing - renews after 2026-11-01")).toBeInTheDocument();
});
it("daily_acceptance_12 Daily quote uses its briefing title and description", async () => {
  getSubscriptionQuote.mockResolvedValue({ action: "subscribe", type: "daily", fee: "2.49", returned: "0.00", balance: "5.00", balance_after: "2.51", sufficient: true, period_start: "2026-10-07", expires_on: "2026-11-07", first_report_at: "2026-10-07T17:00:00-04:00", needs_holdings: false, blocked: null });
  render(<LocaleProvider><ProfilePageBody me={{ ...me, subscription: { ...me.subscription, status: "inactive", type: null } }} hadLoadError={false} /></LocaleProvider>);
  fireEvent.change(screen.getByRole("combobox", { name: "Report schedule" }), { target: { value: "daily" } });
  const dialog = await screen.findByRole("alertdialog");
  expect(within(dialog).getByRole("heading", { name: "Daily briefing" })).toBeInTheDocument();
  expect(within(dialog).getByText("A briefing on your portfolio every weekday, Monday to Friday. Advanced plan: includes AI Agent data access.")).toBeInTheDocument();
  expect(getSubscriptionQuote).toHaveBeenCalledExactlyOnceWith("daily");
});
