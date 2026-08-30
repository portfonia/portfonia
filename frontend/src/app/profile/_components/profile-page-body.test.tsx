import { render, screen, within } from "@testing-library/react";
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
import type { Me, PendingEmailVerification } from "@/lib/api";
import { ProfilePageBody } from "./profile-page-body";

function renderBody(me: Me | null, hadLoadError = false) {
  return render(
    <LocaleProvider>
      <ProfilePageBody me={me} hadLoadError={hadLoadError} />
    </LocaleProvider>,
  );
}

// Section titles render as divs (CardTitle), so order is asserted on
// `data-slot="card-title"` nodes rather than heading roles.
function sectionTitles(): string[] {
  return Array.from(document.querySelectorAll('[data-slot="card-title"]')).map(
    (el) => el.textContent ?? "",
  );
}

function deliveryCard(): HTMLElement {
  const card = screen.getByText("Report delivery email").closest('[data-slot="card"]');
  if (!(card instanceof HTMLElement)) throw new Error("delivery-email card not found");
  return card;
}

const PENDING: PendingEmailVerification = {
  id: "v-1",
  purpose: "account_email",
  email: "new-user@example.com",
  status: "pending",
  expires_at: "2026-09-01T00:00:00Z",
  last_sent_at: "2026-08-30T00:00:00Z",
};

const BASE_ME: Me = {
  email: "user@example.com",
  delivery_email: null,
  email_verified_at: null,
  delivery_email_verified_at: null,
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

describe("Section order (issue #269 §1/§4)", () => {
  it("orders Email Verification right after the gap card, and Change password before Delete account", () => {
    renderBody({ ...BASE_ME, pending_email_verifications: [PENDING] });

    const titles = sectionTitles();
    expect(titles).toEqual([
      "Finish setting up your account",
      "Email verification",
      "Account",
      "Investment style",
      "Report delivery email",
      "Portfolio overview",
      "Report schedule",
      "Invite someone",
      "Change password",
      "Delete account",
    ]);
  });

  it("keeps Email Verification as the first section when the gap card does not render", () => {
    renderBody({
      ...BASE_ME,
      has_questionnaire: true,
      has_holdings: true,
      missing: [],
      pending_email_verifications: [PENDING],
    });

    const titles = sectionTitles();
    expect(titles[0]).toBe("Email verification");
    expect(titles[1]).toBe("Account");
  });
});

describe("Urgency + danger zone styling (issue #269 §2/§5)", () => {
  it("gives the gap card the urgent (pink) variant", () => {
    renderBody(BASE_ME);

    const link = screen.getByRole("link", { name: /set your investment style/i });
    expect(link.closest('[data-variant="urgent"]')).not.toBeNull();
  });

  it("gives the Email Verification section the urgent (pink) variant", () => {
    renderBody({ ...BASE_ME, pending_email_verifications: [PENDING] });

    const title = screen.getByText("Email verification");
    expect(title.closest('[data-variant="urgent"]')).not.toBeNull();
  });

  it("wraps Delete account in the danger (red border) variant, not the urgent fill", () => {
    renderBody(BASE_ME);

    const deleteButton = screen.getByRole("button", { name: "Delete account" });
    const dangerCard = deleteButton.closest('[data-variant="danger"]');
    expect(dangerCard).not.toBeNull();
    // The danger zone is a border-only treatment — no pink fill.
    expect(deleteButton.closest('[data-variant="urgent"]')).toBeNull();
  });
});

describe("Email Verification section render condition (issue #269 §1/§3)", () => {
  it("shows the no-valid-recipient warning when nothing is verified, even with an empty list", () => {
    renderBody(BASE_ME);

    expect(
      screen.getByText(/no verified receiving email address/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /resend/i })).not.toBeInTheDocument();
  });

  it("renders nothing when nothing is pending and at least one address is verified", () => {
    renderBody({ ...BASE_ME, email_verified_at: "2026-08-30T00:00:00Z" });

    expect(screen.queryByText(/email verification/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no verified receiving email address/i)).not.toBeInTheDocument();
  });

  it("lists each pending record with email, purpose and status", () => {
    renderBody({ ...BASE_ME, pending_email_verifications: [PENDING] });

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
          ...PENDING,
          id: "v-2",
          purpose: "delivery_email",
          email: "typo@example.com",
          status: "undeliverable",
        },
      ],
    });

    expect(screen.getByText(/delivery failed/i)).toBeInTheDocument();
  });
});

describe("Delivery email inline unverified treatment (issue #269 §6)", () => {
  it("renders the shown address gray italic with a note and an inline Resend button when unverified with a matching record", () => {
    renderBody({
      ...BASE_ME,
      delivery_email: "reports@example.com",
      email_verified_at: "2026-08-30T00:00:00Z",
      pending_email_verifications: [
        {
          ...PENDING,
          id: "v-3",
          purpose: "delivery_email",
          email: "reports@example.com",
        },
      ],
    });

    const card = deliveryCard();
    expect(within(card).getByText("reports@example.com")).toHaveClass("italic");
    expect(within(card).getByText("This address is not verified.")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: /resend/i })).toBeEnabled();
    // Accepted overlap (issue #269 §6): the top section shows the same
    // record's resend affordance as well.
    expect(screen.getAllByRole("button", { name: /resend/i })).toHaveLength(2);
  });

  it("shows the unverified note without a Resend button when no resendable record exists", () => {
    renderBody({ ...BASE_ME, delivery_email: "reports@example.com" });

    const card = deliveryCard();
    expect(within(card).getByText("This address is not verified.")).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: /resend/i })).not.toBeInTheDocument();
  });

  it("marks the account-email fallback unverified when the account email is unverified", () => {
    renderBody(BASE_ME);

    const card = deliveryCard();
    expect(within(card).getByText("user@example.com")).toHaveClass("italic");
    expect(within(card).getByText("This address is not verified.")).toBeInTheDocument();
  });

  it("renders the address normally with no note or button when the shown address is verified", () => {
    renderBody({
      ...BASE_ME,
      delivery_email: "reports@example.com",
      email_verified_at: "2026-08-30T00:00:00Z",
      delivery_email_verified_at: "2026-08-30T00:00:00Z",
    });

    const card = deliveryCard();
    expect(within(card).getByText("reports@example.com")).not.toHaveClass("italic");
    expect(within(card).queryByText("This address is not verified.")).not.toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: /resend/i })).not.toBeInTheDocument();
  });
});
