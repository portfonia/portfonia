import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { routerRefresh } = vi.hoisted(() => ({ routerRefresh: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/profile",
  useRouter: () => ({ refresh: routerRefresh }),
}));
// Server Action import would drag in lib/supabase/server.ts's `server-only`
// guard under vitest (no Next compiler pass to stub it) — mock like the
// other suites do (see get-started-menu.test.tsx's identical comment).
vi.mock("@/app/profile/actions", () => ({ changePassword: vi.fn() }));
// ProfilePageBody now (indirectly) imports lib/api.ts's resendEmailVerification
// (issue #262), whose module pulls logout() from the server-only-guarded
// Supabase server client — mock it like holdings-manager.test.tsx does.
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
// Partial mock of lib/api.ts so the resend success path can be exercised
// (PR #270 review: no test covered a successful resend re-enabling the
// buttons) — same importActual pattern as questionnaire-form.test.tsx.
// createEmailVerification is the issue #289 sibling flow (fresh verification
// for an account's own known address, no existing record required).
const {
  resendEmailVerification,
  createEmailVerification,
  updateReportLanguage,
  updateReportCurrency,
} = vi.hoisted(() => ({
  resendEmailVerification: vi.fn(),
  createEmailVerification: vi.fn(),
  updateReportLanguage: vi.fn(),
  updateReportCurrency: vi.fn(),
}));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    resendEmailVerification,
    createEmailVerification,
    updateReportLanguage,
    updateReportCurrency,
  };
});

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { Me, PendingEmailVerification } from "@/lib/api";
import { ApiError } from "@/lib/api";
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
  // Issue #289: the noVerifiedRecipient gap card now also renders a
  // "Report delivery email" purpose label (a span), so the title text is
  // no longer unique on the page — pick the element inside a card-title
  // slot, which only the CardTitle has.
  const title = screen
    .getAllByText("Report delivery email")
    .find((el) => el.closest('[data-slot="card-title"]') !== null);
  if (!title) throw new Error("delivery-email card title not found");
  const card = title.closest('[data-slot="card"]');
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
  report_language: "en",
  report_currency: "USD",
};

describe("ProfilePageBody", () => {
  beforeEach(() => {
    resendEmailVerification.mockReset();
    createEmailVerification.mockReset();
    updateReportLanguage.mockReset();
    updateReportCurrency.mockReset();
    routerRefresh.mockReset();
  });

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

    // The address legitimately appears in the delivery card AND (nothing
    // verified) the gap card's recovery row — scope to the delivery card.
    expect(within(deliveryCard()).getByText("reports@example.com")).toBeInTheDocument();
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

    expect(screen.getByRole("combobox", { name: /report schedule/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete account" })).toBeDisabled();
    expect(screen.getAllByText(/not implemented yet/i).length).toBeGreaterThanOrEqual(1);
  });

  it("links the portfolio overview card to /portfolio (issue #320: no longer a placeholder)", () => {
    renderBody(BASE_ME);

    expect(screen.getByRole("link", { name: /view portfolio overview/i })).toHaveAttribute(
      "href",
      "/portfolio",
    );
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
    expect(screen.getByRole("link", { name: /view portfolio overview/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete account" })).toBeDisabled();
  });
});

describe("Section order (issue #269 §1/§4, issue #308)", () => {
  it("orders Portfolio overview before Report delivery email, Report language before Report schedule, and Change password before Delete account", () => {
    renderBody({ ...BASE_ME, pending_email_verifications: [PENDING] });

    const titles = sectionTitles();
    expect(titles).toEqual([
      "Finish setting up your account",
      "Email verification",
      "Account",
      "Investment style",
      "Portfolio overview",
      "Report delivery email",
      "Report language & currency",
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

    // Issue #290: the full new string — first sentence plus the send-stop.
    expect(
      screen.getByText(
        /no verified receiving email address\. reports will not be sent until an address is verified\./i,
      ),
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

  it("shows the no-valid-recipient warning in addition to the pending list when both apply (issue #269 §3)", () => {
    renderBody({ ...BASE_ME, pending_email_verifications: [PENDING] });

    // "in addition to (not instead of)" — the warning renders alongside the
    // list rows, not instead of them (PR #270 review: no test pinned this).
    expect(
      screen.getByText(/no verified receiving email address/i),
    ).toBeInTheDocument();
    expect(screen.getByText("new-user@example.com")).toBeInTheDocument();
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

describe("Resend success path (PR #270 review finding 1)", () => {
  function renderWithInlineResend() {
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
    return deliveryCard();
  }

  it("re-enables the inline resend button after a successful resend", async () => {
    resendEmailVerification.mockResolvedValue(undefined);
    const card = renderWithInlineResend();

    const button = within(card).getByRole("button", { name: /resend/i });
    expect(button).toBeEnabled();
    await userEvent.click(button);
    expect(resendEmailVerification).toHaveBeenCalledWith("v-3");

    // During flight the label switches to "Sending..." (not matched by
    // /resend/i); the button must come back enabled — the success path
    // clears pendingId instead of leaving every button disabled until a
    // full remount.
    await waitFor(() => {
      const after = within(card).getByRole("button", { name: /resend/i });
      expect(after).toBeEnabled();
    });
  });

  it("re-enables the list resend button after a successful resend", async () => {
    resendEmailVerification.mockResolvedValue(undefined);
    renderBody({ ...BASE_ME, pending_email_verifications: [PENDING] });

    const button = screen.getByRole("button", { name: /resend/i });
    await userEvent.click(button);

    await waitFor(() => {
      const after = screen.getByRole("button", { name: /resend/i });
      expect(after).toBeEnabled();
    });
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

describe("No-verified-recipient self-service recovery (issue #289 item 3)", () => {
  beforeEach(() => {
    createEmailVerification.mockReset();
    routerRefresh.mockReset();
  });

  it("shows a Send verification button for the account email when nothing is verified", () => {
    renderBody(BASE_ME);

    // The account email appears in the gap card row, the Account card and
    // the delivery-email fallback row when nothing is verified.
    expect(screen.getAllByText("user@example.com").length).toBeGreaterThanOrEqual(1);
    const accountButton = screen.getByRole("button", { name: /send verification/i });
    expect(accountButton).toBeEnabled();
    // No delivery email set — only the account row gets a button.
    expect(screen.getAllByRole("button", { name: /send verification/i })).toHaveLength(1);
  });

  it("also lists the delivery email with its own Send verification button when set", () => {
    renderBody({ ...BASE_ME, delivery_email: "reports@example.com" });

    // Delivery address appears in the gap card row and the delivery card.
    expect(screen.getAllByText("reports@example.com").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByRole("button", { name: /send verification/i })).toHaveLength(2);
  });

  it("calls createEmailVerification with account_email for the account row", async () => {
    createEmailVerification.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderBody(BASE_ME);

    await user.click(screen.getByRole("button", { name: /send verification/i }));

    expect(createEmailVerification).toHaveBeenCalledWith("account_email");
  });

  it("calls createEmailVerification with delivery_email for the delivery row", async () => {
    createEmailVerification.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderBody({ ...BASE_ME, delivery_email: "reports@example.com" });

    const buttons = screen.getAllByRole("button", { name: /send verification/i });
    await user.click(buttons[1]);

    expect(createEmailVerification).toHaveBeenCalledWith("delivery_email");
  });

  it("refreshes the page data after a successful send (router.refresh, never a hard reload)", async () => {
    createEmailVerification.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderBody(BASE_ME);

    await user.click(screen.getByRole("button", { name: /send verification/i }));

    await waitFor(() => expect(routerRefresh).toHaveBeenCalled());
  });

  it("shows translated error copy on 429/503/other failures", async () => {
    const user = userEvent.setup();
    renderBody(BASE_ME);
    const button = () => screen.getByRole("button", { name: /send verification/i });

    createEmailVerification.mockRejectedValue(new ApiError(429, "too many"));
    await user.click(button());
    expect(await screen.findByRole("alert")).toHaveTextContent(/too many verification requests/i);

    createEmailVerification.mockRejectedValue(new ApiError(503, "unavailable"));
    await user.click(button());
    expect(await screen.findByRole("alert")).toHaveTextContent(/temporarily unavailable/i);

    createEmailVerification.mockRejectedValue(new ApiError(500, "boom"));
    await user.click(button());
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not send the verification email/i);
  });

});

describe("Report language (issue #308)", () => {
  it("renders a real, non-disabled selector defaulted to the account's current value", () => {
    renderBody({ ...BASE_ME, report_language: "zh" });

    const select = screen.getByRole("combobox", { name: /report language/i });
    expect(select).toBeEnabled();
    expect(select).toHaveValue("zh");
  });

  it("offers exactly English and Simplified Chinese, in their own native script", () => {
    renderBody(BASE_ME);

    const select = screen.getByRole("combobox", { name: /report language/i });
    const options = within(select)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(options).toEqual(["English", "简体中文"]);
  });

  it("calls updateReportLanguage immediately on change, then router.refresh (no Save button, no hard reload)", async () => {
    updateReportLanguage.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderBody({ ...BASE_ME, report_language: "en" });

    const select = screen.getByRole("combobox", { name: /report language/i });
    await user.selectOptions(select, "zh");

    expect(updateReportLanguage).toHaveBeenCalledWith("zh");
    await waitFor(() => expect(routerRefresh).toHaveBeenCalled());
    expect(
      screen.queryByRole("button", { name: /save/i }),
    ).not.toBeInTheDocument();
  });

  it("shows an error and leaves the control enabled when the update fails", async () => {
    updateReportLanguage.mockRejectedValue(new ApiError(500, "boom"));
    const user = userEvent.setup();
    renderBody({ ...BASE_ME, report_language: "en" });

    const select = screen.getByRole("combobox", { name: /report language/i });
    await user.selectOptions(select, "zh");

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(select).toBeEnabled();
  });
});

describe("Report currency (issue #350 item 1)", () => {
  it("renders a real, non-disabled selector defaulted to the account's current value", () => {
    renderBody({ ...BASE_ME, report_currency: "CNY" });

    const select = screen.getByRole("combobox", { name: /report currency/i });
    expect(select).toBeEnabled();
    expect(select).toHaveValue("CNY");
  });

  it("offers all 15 VALID_CURRENCIES", () => {
    renderBody(BASE_ME);

    const select = screen.getByRole("combobox", { name: /report currency/i });
    const options = within(select)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(options).toHaveLength(15);
    expect(options).toContain("USD");
    expect(options).toContain("CNY");
  });

  it("calls updateReportCurrency immediately on change, then router.refresh", async () => {
    updateReportCurrency.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderBody({ ...BASE_ME, report_currency: "USD" });

    const select = screen.getByRole("combobox", { name: /report currency/i });
    await user.selectOptions(select, "CNY");

    expect(updateReportCurrency).toHaveBeenCalledWith("CNY");
    await waitFor(() => expect(routerRefresh).toHaveBeenCalled());
  });

  it("shows an error and leaves the control enabled when the update fails", async () => {
    updateReportCurrency.mockRejectedValue(new ApiError(500, "boom"));
    const user = userEvent.setup();
    renderBody({ ...BASE_ME, report_currency: "USD" });

    const select = screen.getByRole("combobox", { name: /report currency/i });
    await user.selectOptions(select, "CNY");

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(select).toBeEnabled();
  });
});

describe("Send verification button hiding", () => {
  it("hides Send verification for a purpose that already has an actionable row (PR #292 review)", () => {
    // Post-signup state: account email unverified, its pending row already
    // live (email matches me.email) — Send would supersede the token in
    // the inbox; Resend in the list below stays the only action.
    renderBody({
      ...BASE_ME,
      pending_email_verifications: [{ ...PENDING, id: "v-4", email: "user@example.com" }],
    });

    expect(screen.queryByRole("button", { name: /send verification/i })).not.toBeInTheDocument();
    // Resend stays the only action: the pending-list row AND the
    // delivery-card inline resend both target the same live record (the
    // §9.7/list overlap is documented as intended).
    expect(screen.getAllByRole("button", { name: /resend/i }).length).toBeGreaterThanOrEqual(1);
  });

  it("keeps Send verification for a purpose without an actionable row when the other purpose has one", () => {
    renderBody({
      ...BASE_ME,
      delivery_email: "reports@example.com",
      pending_email_verifications: [{ ...PENDING, id: "v-5", email: "user@example.com" }],
    });

    // Account row hidden (its pending row exists); delivery row keeps Send.
    const sendButtons = screen.getAllByRole("button", { name: /send verification/i });
    expect(sendButtons).toHaveLength(1);
    // Delivery address appears in the gap card row and the delivery card.
    expect(screen.getAllByText("reports@example.com").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByRole("button", { name: /resend/i }).length).toBeGreaterThanOrEqual(1);
  });
});
