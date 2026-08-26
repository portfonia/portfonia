import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const { login, markPendingLogin, clearPendingLogin } = vi.hoisted(() => ({
  login: vi.fn(),
  markPendingLogin: vi.fn(),
  clearPendingLogin: vi.fn(),
}));

vi.mock("./actions", () => ({ login }));
vi.mock("@/hooks/use-session", () => ({ markPendingLogin, clearPendingLogin }));

import { LoginForm } from "./login-form";

describe("LoginForm", () => {
  it("submits the entered email and password to the login action", async () => {
    login.mockResolvedValue({ error: null });
    const user = userEvent.setup();
    render(<LoginForm />);

    await user.type(screen.getByLabelText(/email/i), "a@b.com");
    await user.type(screen.getByLabelText(/password/i), "correcthorse");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    await waitFor(() => expect(login).toHaveBeenCalled());
    const submittedForm = login.mock.calls[0][1] as FormData;
    expect(submittedForm.get("email")).toBe("a@b.com");
    expect(submittedForm.get("password")).toBe("correcthorse");
  });

  it("marks the login as pending on submit, before the Server Action resolves", async () => {
    // The next page's useSession instance (SiteHeader lives in the shared
    // root layout and has already navigated away by the time login()
    // resolves) reads this signal to show "Logging in..." instead of a
    // blank menu spot during the post-redirect verification round-trip
    // (issue #214 follow-up).
    login.mockResolvedValue({ error: null });
    const user = userEvent.setup();
    render(<LoginForm />);

    await user.type(screen.getByLabelText(/email/i), "a@b.com");
    await user.type(screen.getByLabelText(/password/i), "correcthorse");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    expect(markPendingLogin).toHaveBeenCalled();
  });

  it("shows the error message returned by the action", async () => {
    login.mockResolvedValue({ error: "Invalid email or password." });
    const user = userEvent.setup();
    render(<LoginForm />);

    await user.type(screen.getByLabelText(/email/i), "a@b.com");
    await user.type(screen.getByLabelText(/password/i), "wrong");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    expect(await screen.findByText("Invalid email or password.")).toBeInTheDocument();
  });

  it("clears the pending-login signal when the action returns an error", async () => {
    login.mockResolvedValue({ error: "Invalid email or password." });
    const user = userEvent.setup();
    render(<LoginForm />);

    await user.type(screen.getByLabelText(/email/i), "a@b.com");
    await user.type(screen.getByLabelText(/password/i), "wrong");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    await screen.findByText("Invalid email or password.");
    expect(clearPendingLogin).toHaveBeenCalled();
  });

  it("tells an account-less visitor to ask for an invite, rather than linking to a token-less /signup", () => {
    render(<LoginForm />);

    expect(screen.getByText(/need an invite/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /sign ?up/i })).not.toBeInTheDocument();
  });
});
