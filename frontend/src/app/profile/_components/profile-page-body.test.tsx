import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/profile", useRouter: () => ({ refresh: vi.fn() }) }));
// Server Action import would drag in lib/supabase/server.ts's `server-only`
// guard under vitest (no Next compiler pass to stub it) — mock like the
// other suites do (see get-started-menu.test.tsx's identical comment).
vi.mock("@/app/profile/actions", () => ({ changePassword: vi.fn() }));
// ProfilePageBody now (indirectly) imports lib/api.ts's resendEmailVerification
// (issue #262), whose module pulls logout() from the server-only-guarded
// Supabase server client — mock it like holdings-manager.test.tsx does.
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { Me } from "@/lib/api";
import { ProfilePageBody } from "./profile-page-body";

function renderBody(me: Me | null, hadLoadError = false) {
  return render(
    <LocaleProvider>
      <ProfilePageBody me={me} hadLoadError={hadLoadError} />
    </LocaleProvider>,
  );
}

const BASE_ME: Me = {
  email: "user@example.com",
  delivery_email: null,
  tos_accepted_at: null,
  has_questionnaire: false,
  has_holdings: false,
  missing: ["questionnaire", "holdings"],
  pending_email_verifications: [],
};

describe("ProfilePageBody", () => {
  it("shows a load error and nothing else when the fetch failed", () => {
    renderBody(null, true);

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("user@example.com")).not.toBeInTheDocument();
  });

  it("shows the account email read-only", () => {
    renderBody(BASE_ME);

    // BASE_ME has no delivery_email, so the account email legitimately
    // appears twice (account row + delivery-email fallback row).
    expect(screen.getAllByText("user@example.com").length).toBeGreaterThanOrEqual(1);
  });

  it("falls back to the account email for delivery email, with a note, when unset", () => {
    renderBody({ ...BASE_ME, delivery_email: null });

    const emails = screen.getAllByText("user@example.com");
    expect(emails.length).toBeGreaterThanOrEqual(2); // account row + delivery-email row
    expect(
      screen.getByText(/no separate delivery address set/i),
    ).toBeInTheDocument();
  });

  it("shows the real delivery email with no fallback note when it is set", () => {
    renderBody({ ...BASE_ME, delivery_email: "reports@example.com" });

    expect(screen.getByText("reports@example.com")).toBeInTheDocument();
    expect(screen.queryByText(/no separate delivery address set/i)).not.toBeInTheDocument();
  });

  it("links the investment style button to /questionnaire in default mode (no onboarding query)", () => {
    renderBody(BASE_ME);

    expect(screen.getByRole("link", { name: /update investment style/i })).toHaveAttribute(
      "href",
      "/questionnaire",
    );
  });

  it("renders every placeholder section as visibly non-interactive", () => {
    renderBody(BASE_ME);

    expect(screen.getByText(/portfolio overview/i)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /report schedule/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete account" })).toBeDisabled();
    expect(screen.getAllByText(/not implemented yet/i).length).toBeGreaterThanOrEqual(1);
  });

  it("renders the Change password form", () => {
    renderBody(BASE_ME);

    expect(screen.getByLabelText(/current password/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^new password$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/confirm new password/i)).toBeInTheDocument();
  });

  it("renders a gap card with a button per missing item, neither carrying ?onboarding=1 (issue #221 §2.6)", () => {
    renderBody({ ...BASE_ME, missing: ["questionnaire", "holdings"] });

    const questionnaireLink = screen.getByRole("link", { name: /set your investment style/i });
    expect(questionnaireLink).toHaveAttribute("href", "/questionnaire");
    const holdingsLink = screen.getByRole("link", { name: /add your holdings/i });
    expect(holdingsLink).toHaveAttribute("href", "/holdings");
  });

  it("shows only the button for the item still missing when one is already done", () => {
    renderBody({ ...BASE_ME, has_questionnaire: true, missing: ["holdings"] });

    expect(
      screen.queryByRole("link", { name: /set your investment style/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /add your holdings/i })).toBeInTheDocument();
  });

  it("renders no gap card at all when `missing` is empty", () => {
    renderBody({ ...BASE_ME, has_questionnaire: true, has_holdings: true, missing: [] });

    expect(
      screen.queryByRole("link", { name: /set your investment style/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /add your holdings/i })).not.toBeInTheDocument();
    // The rest of the page still renders — an empty `missing` doesn't hide
    // anything else.
    expect(screen.getByText(/portfolio overview/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete account" })).toBeDisabled();
  });
});

describe("PendingVerificationsList section (issue #262)", () => {
  it("renders no verification card when pending_email_verifications is empty", () => {
    renderBody(BASE_ME);

    expect(screen.queryByText(/email verification/i)).not.toBeInTheDocument();
  });

  it("lists each pending record with email, purpose and status", () => {
    renderBody({
      ...BASE_ME,
      pending_email_verifications: [
        {
          id: "v-1",
          purpose: "account_email",
          email: "new-user@example.com",
          status: "pending",
          expires_at: "2026-09-01T00:00:00Z",
          last_sent_at: "2026-08-30T00:00:00Z",
        },
      ],
    });

    expect(screen.getByText("new-user@example.com")).toBeInTheDocument();
    expect(
      screen.getByText(/^waiting for your confirmation$/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/^used for: account email$/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /resend/i })).toBeEnabled();
  });

  it("marks an undeliverable record with the delivery-failed wording", () => {
    renderBody({
      ...BASE_ME,
      pending_email_verifications: [
        {
          id: "v-2",
          purpose: "delivery_email",
          email: "typo@example.com",
          status: "undeliverable",
          expires_at: "2026-09-01T00:00:00Z",
          last_sent_at: "2026-08-30T00:00:00Z",
        },
      ],
    });

    expect(screen.getByText(/delivery failed/i)).toBeInTheDocument();
  });
});
