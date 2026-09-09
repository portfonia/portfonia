import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { changePassword } = vi.hoisted(() => ({
  changePassword: vi.fn(),
}));

vi.mock("./actions", () => ({ changePassword }));
// Same rationale as forgot-password-form.test.tsx: jsdom never executes the
// vendored public/altcha.js, so next/script is stubbed and the custom
// element renders as an empty tag — enough to exercise this form's own
// state transitions without a real PoW solve. The mocked action below
// doesn't validate the (empty) altcha field itself, unlike the real one.
vi.mock("next/script", () => ({ default: () => null }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { ChangePasswordForm } from "./change-password-form";

function renderForm() {
  return render(
    <LocaleProvider>
      <ChangePasswordForm />
    </LocaleProvider>,
  );
}

describe("ChangePasswordForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("links back to profile and renders current/new/confirm fields plus the widget host", () => {
    renderForm();

    expect(screen.getByRole("link", { name: /back to profile/i })).toHaveAttribute(
      "href",
      "/profile",
    );
    expect(screen.getByRole("heading", { name: /change password/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/current password/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^new password$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/confirm new password/i)).toBeInTheDocument();
    expect(document.querySelector("altcha-widget")).not.toBeNull();
  });

  it("submits the three password fields to the action", async () => {
    changePassword.mockResolvedValue({ error: null, success: true });
    const user = userEvent.setup();
    renderForm();

    await user.type(screen.getByLabelText(/current password/i), "oldpassword");
    await user.type(screen.getByLabelText(/^new password$/i), "newpassword1");
    await user.type(screen.getByLabelText(/confirm new password/i), "newpassword1");
    await user.click(screen.getByRole("button", { name: /^change password$/i }));

    await waitFor(() => expect(changePassword).toHaveBeenCalled());
    const submittedForm = changePassword.mock.calls[0][1] as FormData;
    expect(submittedForm.get("currentPassword")).toBe("oldpassword");
    expect(submittedForm.get("newPassword")).toBe("newpassword1");
    expect(submittedForm.get("confirmNewPassword")).toBe("newpassword1");
  });

  it("shows the success message once the action reports success", async () => {
    changePassword.mockResolvedValue({ error: null, success: true });
    const user = userEvent.setup();
    renderForm();

    await user.type(screen.getByLabelText(/current password/i), "oldpassword");
    await user.type(screen.getByLabelText(/^new password$/i), "newpassword1");
    await user.type(screen.getByLabelText(/confirm new password/i), "newpassword1");
    await user.click(screen.getByRole("button", { name: /^change password$/i }));

    expect(await screen.findByRole("status")).toHaveTextContent(/password changed/i);
  });

  it("shows an error and keeps the form when the action returns one", async () => {
    changePassword.mockResolvedValue({
      error: "Please complete the verification widget before submitting.",
      success: false,
    });
    const user = userEvent.setup();
    renderForm();

    await user.type(screen.getByLabelText(/current password/i), "oldpassword");
    await user.type(screen.getByLabelText(/^new password$/i), "newpassword1");
    await user.type(screen.getByLabelText(/confirm new password/i), "newpassword1");
    await user.click(screen.getByRole("button", { name: /^change password$/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /complete the verification widget/i,
    );
    expect(screen.getByLabelText(/current password/i)).toBeInTheDocument();
  });
});
