import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { confirmEmailVerification } = vi.hoisted(() => ({
  confirmEmailVerification: vi.fn(),
}));

vi.mock("./actions", () => ({ confirmEmailVerification }));
// Same rationale as forgot-password-form.test.tsx: jsdom never executes the
// vendored public/altcha.js, so next/script is stubbed and the custom
// element renders as an empty tag — enough to exercise this form's own
// state transitions without a real PoW solve. The mocked action below
// doesn't validate the (empty) altcha field itself, unlike the real one.
vi.mock("next/script", () => ({ default: () => null }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { VerifyEmailStatus } from "./page";
import { VerifyEmailForm } from "./verify-email-form";

function renderForm(status: VerifyEmailStatus, token = "tok-1") {
  return render(
    <LocaleProvider>
      <VerifyEmailForm token={token} status={status} />
    </LocaleProvider>,
  );
}

describe("VerifyEmailForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows the invalid/expired message and no form when the token isn't found", () => {
    renderForm({ found: false, status: null, email: null });

    expect(screen.getByRole("alert")).toHaveTextContent(/invalid or has expired/i);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows the invalid/expired message for an expired token", () => {
    renderForm({ found: true, status: "expired", email: "a@b.com" });

    expect(screen.getByRole("alert")).toHaveTextContent(/invalid or has expired/i);
  });

  it("shows the already-verified message and no form for a token already used", () => {
    renderForm({ found: true, status: "verified", email: "a@b.com" });

    expect(screen.getByRole("status")).toHaveTextContent("a@b.com");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders the confirm form for a pending token, submitting the token", async () => {
    confirmEmailVerification.mockResolvedValue({ error: null, email: "a@b.com" });
    const user = userEvent.setup();
    renderForm({ found: true, status: "pending", email: "a@b.com" }, "tok-1");

    await user.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() => expect(confirmEmailVerification).toHaveBeenCalled());
    const submittedForm = confirmEmailVerification.mock.calls[0][1] as FormData;
    expect(submittedForm.get("token")).toBe("tok-1");
  });

  it("shows the success message once the backend confirms", async () => {
    confirmEmailVerification.mockResolvedValue({ error: null, email: "a@b.com" });
    const user = userEvent.setup();
    renderForm({ found: true, status: "pending", email: "a@b.com" });

    await user.click(screen.getByRole("button", { name: /confirm/i }));

    expect(await screen.findByRole("status")).toHaveTextContent("a@b.com");
  });

  it("shows a translated error and keeps the form when the action rejects", async () => {
    confirmEmailVerification.mockResolvedValue({ error: "invalidOrExpired" });
    const user = userEvent.setup();
    renderForm({ found: true, status: "pending", email: "a@b.com" });

    await user.click(screen.getByRole("button", { name: /confirm/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/invalid or has expired/i);
    expect(screen.getByRole("button", { name: /confirm/i })).toBeInTheDocument();
  });
});
