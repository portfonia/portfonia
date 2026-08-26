import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const { signup, markPendingLogin, clearPendingLogin } = vi.hoisted(() => ({
  signup: vi.fn(),
  markPendingLogin: vi.fn(),
  clearPendingLogin: vi.fn(),
}));

vi.mock("./actions", () => ({ signup }));
vi.mock("@/hooks/use-session", () => ({ markPendingLogin, clearPendingLogin }));

import { SignupForm } from "./signup-form";

describe("SignupForm", () => {
  it("shows a missing-invite message and no form when there is no invite token", () => {
    render(<SignupForm inviteToken="" />);

    expect(screen.getByText(/missing its invite token/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/email/i)).not.toBeInTheDocument();
  });

  it("submits the invite token, email, and password to the signup action", async () => {
    signup.mockResolvedValue({ error: null });
    const user = userEvent.setup();
    render(<SignupForm inviteToken="tok-abc" />);

    await user.type(screen.getByLabelText(/email/i), "a@b.com");
    await user.type(screen.getByLabelText(/password/i), "correcthorse");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => expect(signup).toHaveBeenCalled());
    const submittedForm = signup.mock.calls[0][1] as FormData;
    expect(submittedForm.get("invite_token")).toBe("tok-abc");
    expect(submittedForm.get("email")).toBe("a@b.com");
    expect(submittedForm.get("password")).toBe("correcthorse");
  });

  it("shows the error message returned by the action (e.g. an expired invite)", async () => {
    signup.mockResolvedValue({ error: "This invite is no longer valid." });
    const user = userEvent.setup();
    render(<SignupForm inviteToken="tok-abc" />);

    await user.type(screen.getByLabelText(/email/i), "a@b.com");
    await user.type(screen.getByLabelText(/password/i), "correcthorse");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    expect(await screen.findByText("This invite is no longer valid.")).toBeInTheDocument();
  });

  it("marks the session as pending on submit, same signal as login (post-signup redirect to /holdings)", async () => {
    signup.mockResolvedValue({ error: null });
    const user = userEvent.setup();
    render(<SignupForm inviteToken="tok-abc" />);

    await user.type(screen.getByLabelText(/email/i), "a@b.com");
    await user.type(screen.getByLabelText(/password/i), "correcthorse");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    expect(markPendingLogin).toHaveBeenCalled();
  });

  it("clears the pending-login signal when the action returns an error", async () => {
    signup.mockResolvedValue({ error: "This invite is no longer valid." });
    const user = userEvent.setup();
    render(<SignupForm inviteToken="tok-abc" />);

    await user.type(screen.getByLabelText(/email/i), "a@b.com");
    await user.type(screen.getByLabelText(/password/i), "correcthorse");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    await screen.findByText("This invite is no longer valid.");
    expect(clearPendingLogin).toHaveBeenCalled();
  });
});
