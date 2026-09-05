import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { replace } = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { Me } from "@/lib/api";
import { WelcomeBody } from "./welcome-body";

const _ME: Me = {
  email: "a@b.com",
  delivery_email: null,
  email_verified_at: null,
  delivery_email_verified_at: null,
  tos_accepted_at: "2026-08-27T00:00:00Z",
  has_questionnaire: true,
  has_holdings: false,
  missing: ["holdings"],
  pending_email_verifications: [],
  report_language: "en",
  report_currency: "USD",
};

function renderBody(me: Me | null, hadLoadError = false) {
  return render(
    <LocaleProvider>
      <WelcomeBody me={me} hadLoadError={hadLoadError} />
    </LocaleProvider>,
  );
}

describe("WelcomeBody", () => {
  beforeEach(() => {
    sessionStorage.clear();
    replace.mockClear();
  });
  afterEach(() => {
    sessionStorage.clear();
  });

  it("greets by email and shows the without-holdings copy, falling back to the account email", () => {
    renderBody(_ME);
    expect(screen.getByText("Welcome, a@b.com.")).toBeInTheDocument();
    expect(
      screen.getByText("Holdings-related sections stay empty until you save holdings."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Your cadence is weekly/)).toBeInTheDocument();
    // Must never claim a holdings-confirmation email was sent (Ring
    // 1-Onboarding.md §2.4) or print the stale MWF 17:00 schedule.
    expect(screen.queryByText(/has been sent/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/17:00/)).not.toBeInTheDocument();
  });

  it("shows the with-holdings copy when holdings are saved", () => {
    renderBody({ ..._ME, has_holdings: true, delivery_email: "reports@b.com" });
    expect(screen.getByText("Your holdings are saved.")).toBeInTheDocument();
  });

  it("claims no delivery until the fallback account email is verified (issue #290)", () => {
    renderBody(_ME);
    expect(
      screen.getByText("Reports will not be sent until a@b.com is verified."),
    ).toBeInTheDocument();
    // The unverified path must never claim a report is being sent.
    expect(screen.queryByText(/Reports will be sent/)).not.toBeInTheDocument();
  });

  it("claims no delivery when the delivery email is set but unverified and the account email is unverified", () => {
    renderBody({ ..._ME, delivery_email: "reports@b.com" });
    expect(
      screen.getByText("Reports will not be sent until reports@b.com is verified."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Reports will be sent/)).not.toBeInTheDocument();
  });

  it("does not claim a send-stop when the delivery email is unverified but the account email is verified (PR #294 review)", () => {
    // Layer 2 (recipient_email_with_purpose) prefers a verified delivery
    // address, else a verified account email; an unverified delivery_email
    // is skipped, so this mixed state still sends to the account address.
    renderBody({
      ..._ME,
      delivery_email: "reports@b.com",
      email_verified_at: "2026-08-27T00:00:00Z",
    });
    expect(screen.getByText("Reports will be sent to a@b.com.")).toBeInTheDocument();
    expect(screen.queryByText(/will not be sent/)).not.toBeInTheDocument();
  });

  it("says reports will be sent once the fallback account email is verified", () => {
    renderBody({ ..._ME, email_verified_at: "2026-08-27T00:00:00Z" });
    expect(screen.getByText("Reports will be sent to a@b.com.")).toBeInTheDocument();
    // The verified path must not claim send is blocked.
    expect(screen.queryByText(/will not be sent/)).not.toBeInTheDocument();
  });

  it("says reports will be sent once the delivery email itself is verified", () => {
    renderBody({
      ..._ME,
      delivery_email: "reports@b.com",
      delivery_email_verified_at: "2026-08-27T00:00:00Z",
    });
    expect(screen.getByText("Reports will be sent to reports@b.com.")).toBeInTheDocument();
    expect(screen.queryByText(/will not be sent/)).not.toBeInTheDocument();
  });

  it("has no dashboard button and no CTA", () => {
    renderBody(_ME);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows the load-error message when me could not be loaded", () => {
    renderBody(null, true);
    expect(screen.getByText("Could not load your account.")).toBeInTheDocument();
  });

  it("sets sessionStorage.portfonia.welcomed on first render", () => {
    renderBody(_ME);
    expect(sessionStorage.getItem("portfonia.welcomed")).toBe("1");
  });

  it("does not burn the one-shot flag when the load failed (blacktomb42 review, PR #230)", () => {
    renderBody(null, true);
    expect(sessionStorage.getItem("portfonia.welcomed")).toBeNull();
  });

  it("redirects to / instead of rendering when already welcomed this session", () => {
    sessionStorage.setItem("portfonia.welcomed", "1");
    renderBody(_ME);
    expect(replace).toHaveBeenCalledWith("/");
    expect(screen.queryByText(/Welcome,/)).not.toBeInTheDocument();
  });
});
