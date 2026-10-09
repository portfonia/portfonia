import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/hooks/use-session", () => ({ revalidateSession: vi.fn() }));
vi.mock("next-intl", () => ({ useLocale: () => "en", useTranslations: () => (key: string) => key }));
vi.mock("./credit-purchase", () => ({ CreditPurchase: () => null }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
import type { Me } from "@/lib/api";
import { ProfilePageBody } from "./profile-page-body";
const me: Me = { email: "jade@example.com", credit_balance: "10.00", delivery_email: null, email_verified_at: "2026-10-01T12:00:00Z", delivery_email_verified_at: null, tos_accepted_at: null, has_questionnaire: true, has_holdings: false, missing: [], pending_email_verifications: [], report_language: "en", report_currency: "USD", subscription: { status: "active", type: "jade", cadence: "mwf", expires_on: "2026-11-16", cancel_pending: false, next_adjustment_at: null } };
it.each([false, true])("A10 Jade Profile disables plan and routes management; pending=%s", (cancel_pending) => {
  render(<ProfilePageBody me={{ ...me, subscription: { ...me.subscription, cancel_pending } }} hadLoadError={false} />);
  const select = screen.getByRole("combobox", { name: "reportScheduleHeading" });
  expect(select).toBeDisabled(); expect(select).toHaveValue("mwf");
  expect(screen.queryByRole("button", { name: "subscriptionCancel" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "subscriptionResume" })).not.toBeInTheDocument();
  for (const link of screen.getAllByRole("link", { name: "jadeManage" })) expect(link).toHaveAttribute("href", "/jade");
  expect(screen.queryByRole("option", { name: "subscriptionTitle.jade" })).not.toBeInTheDocument();
});
it("A10 Daily remains adjustable", () => {
  render(<ProfilePageBody me={{ ...me, subscription: { ...me.subscription, type: "daily", cadence: "daily" } }} hadLoadError={false} />);
  expect(screen.getByRole("combobox", { name: "reportScheduleHeading" })).toBeEnabled();
  expect(screen.getByRole("combobox", { name: "reportScheduleHeading" })).toHaveValue("daily");
  expect(screen.getByRole("button", { name: "subscriptionCancel" })).toBeEnabled();
});
it("D10.8 cancelled subscription re-enables Profile", () => {
  render(<ProfilePageBody me={{ ...me, subscription: { ...me.subscription, status: "cancelled", type: null, cadence: "none" } }} hadLoadError={false} />);
  expect(screen.getByRole("combobox", { name: "reportScheduleHeading" })).toBeEnabled();
});
it("375px component check keeps Jade plan full width and account row wrapping", () => {
  vi.stubGlobal("innerWidth", 375);
  render(<div style={{ width: 375 }}><ProfilePageBody me={me} hadLoadError={false} /></div>);
  expect(window.innerWidth).toBe(375);
  expect(screen.getByRole("combobox", { name: "reportScheduleHeading" })).toHaveClass("w-full");
  expect(screen.getAllByRole("link", { name: "jadeManage" })[0].parentElement).toHaveClass("flex-wrap");
  vi.unstubAllGlobals();
});
