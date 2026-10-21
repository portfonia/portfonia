import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { Me } from "@/lib/api";

const { tracking } = vi.hoisted(() => ({ tracking: vi.fn() }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("next-intl", () => ({ useTranslations: (namespace: string) => (key: string) => `${namespace}.${key}` }));
vi.mock("@/app/_components/locale-provider", () => ({ useLocalizedHref: () => (path: string) => path, useLocale: () => ({ locale: "en" }) }));
vi.mock("../_components/tracking-section", () => ({ TrackingSection: ({ settings }: { settings: { currency: string; range: string } }) => { tracking(settings); return <div data-testid="tracking-section" />; } }));
import { TrackingBody } from "./tracking-body";
const me: Me = { email: "jade@example.com", credit_balance: "10.00", delivery_email: null, email_verified_at: null, delivery_email_verified_at: null, tos_accepted_at: null, has_questionnaire: true, has_holdings: true, missing: [], pending_email_verifications: [], report_language: "en", report_currency: "HKD", subscription: { status: "active", type: "jade", cadence: "weekly", expires_on: null, cancel_pending: false, next_adjustment_at: null } };
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it("808 A2 gives tracking independent report-currency and span settings", () => {
  render(<TrackingBody me={me} hadLoadError={false} />);
  expect(tracking).toHaveBeenLastCalledWith({ currency: "HKD", range: "1Y" });
  fireEvent.click(screen.getByRole("button", { name: "jade.replay.ranges.3Y" }));
  expect(tracking).toHaveBeenLastCalledWith({ currency: "HKD", range: "3Y" });
});
it("808 A3 non-Jade sees access copy and Jade link without tracking", () => {
  render(<TrackingBody me={{ ...me, subscription: { ...me.subscription, type: "daily" } }} hadLoadError={false} />);
  expect(screen.getByText("jade.whatIf.access")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "jade.whatIf.jadeLink" })).toHaveAttribute("href", "/jade");
  expect(screen.queryByTestId("tracking-section")).not.toBeInTheDocument();
  expect(tracking).not.toHaveBeenCalled();
});
