import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/profile" }));
// Server Action import would drag in lib/supabase/server.ts's `server-only`
// guard under vitest (no Next compiler pass to stub it) — mock like the
// other suites do (see get-started-menu.test.tsx's identical comment).
vi.mock("@/app/profile/actions", () => ({ changePassword: vi.fn() }));

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
});
