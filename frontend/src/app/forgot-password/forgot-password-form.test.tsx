import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { forgotPassword } = vi.hoisted(() => ({
  forgotPassword: vi.fn(),
}));

vi.mock("./actions", () => ({ forgotPassword }));
// The Altcha widget is a self-hosted custom element loaded via next/script at
// runtime (jsdom never executes public/altcha.js) — stub next/script and the
// custom element renders as an empty tag, which is enough to exercise the
// form's own logic without a real PoW solve.
vi.mock("next/script", () => ({ default: () => null }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { ForgotPasswordForm } from "./forgot-password-form";

function renderForm() {
  return render(
    <LocaleProvider>
      <ForgotPasswordForm />
    </LocaleProvider>,
  );
}

describe("ForgotPasswordForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("submits the email to the action", async () => {
    forgotPassword.mockResolvedValue({ error: null, accountFound: true });
    const user = userEvent.setup();
    renderForm();

    await user.type(screen.getByLabelText(/^email$/i), "a@b.com");
    await user.click(screen.getByRole("button", { name: /send reset link/i }));

    await waitFor(() => expect(forgotPassword).toHaveBeenCalled());
    const submittedForm = forgotPassword.mock.calls[0][1] as FormData;
    expect(submittedForm.get("email")).toBe("a@b.com");
  });

  it("shows the explicit account-found message once the backend answers", async () => {
    forgotPassword.mockResolvedValue({ error: null, accountFound: true });
    const user = userEvent.setup();
    renderForm();

    await user.type(screen.getByLabelText(/^email$/i), "known@b.com");
    await user.click(screen.getByRole("button", { name: /send reset link/i }));

    expect(await screen.findByRole("status")).toHaveTextContent(/reset link has been sent/i);
  });

  it("shows the explicit account-not-found message (issue #231's deliberate enumeration deviation)", async () => {
    forgotPassword.mockResolvedValue({ error: null, accountFound: false });
    const user = userEvent.setup();
    renderForm();

    await user.type(screen.getByLabelText(/^email$/i), "nobody@b.com");
    await user.click(screen.getByRole("button", { name: /send reset link/i }));

    expect(await screen.findByRole("status")).toHaveTextContent(/no account was found/i);
  });

  it("shows an error and keeps the form when the action returns one", async () => {
    forgotPassword.mockResolvedValue({ error: "too many attempts, try again later" });
    const user = userEvent.setup();
    renderForm();

    await user.type(screen.getByLabelText(/^email$/i), "a@b.com");
    await user.click(screen.getByRole("button", { name: /send reset link/i }));

    expect(await screen.findByText("too many attempts, try again later")).toBeInTheDocument();
    expect(screen.getByLabelText(/^email$/i)).toBeInTheDocument();
  });
});
