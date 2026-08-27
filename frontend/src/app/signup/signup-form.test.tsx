import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { signup, markPendingLogin, clearPendingLogin } = vi.hoisted(() => ({
  signup: vi.fn(),
  markPendingLogin: vi.fn(),
  clearPendingLogin: vi.fn(),
}));

vi.mock("./actions", () => ({ signup }));
vi.mock("@/hooks/use-session", () => ({ markPendingLogin, clearPendingLogin }));

import { SignupForm } from "./signup-form";

const email = () => screen.getByLabelText(/^email$/i);
const password = () => screen.getByLabelText(/^password$/i);
const confirmPassword = () => screen.getByLabelText(/^confirm password$/i);

async function fillForm(user: ReturnType<typeof userEvent.setup>, pw: string) {
  await user.type(email(), "a@b.com");
  await user.type(password(), pw);
  await user.type(confirmPassword(), pw);
}

describe("SignupForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a missing-invite message and no form when there is no invite token", () => {
    render(<SignupForm inviteToken="" />);

    expect(screen.getByText(/missing its invite token/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/email/i)).not.toBeInTheDocument();
  });

  it("submits the invite token, email, and password to the signup action", async () => {
    signup.mockResolvedValue({ error: null });
    const user = userEvent.setup();
    render(<SignupForm inviteToken="tok-abc" />);

    await fillForm(user, "correcthorse");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => expect(signup).toHaveBeenCalled());
    const submittedForm = signup.mock.calls[0][1] as FormData;
    expect(submittedForm.get("invite_token")).toBe("tok-abc");
    expect(submittedForm.get("email")).toBe("a@b.com");
    expect(submittedForm.get("password")).toBe("correcthorse");
  });

  it("blocks submit when the passwords do not match and shows the mismatch message", async () => {
    signup.mockResolvedValue({ error: null });
    const user = userEvent.setup();
    render(<SignupForm inviteToken="tok-abc" />);

    await user.type(email(), "a@b.com");
    await user.type(password(), "correcthorse");
    await user.type(confirmPassword(), "wronghorse");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    expect(await screen.findByText("Passwords do not match.")).toBeInTheDocument();
    expect(signup).not.toHaveBeenCalled();
    // Arming the pending-login signal here would leave it stuck: the action
    // never runs, so settle-auth-action can never disarm it.
    expect(markPendingLogin).not.toHaveBeenCalled();
  });

  it("clears the mismatch flag and submits once the values agree again", async () => {
    signup.mockResolvedValue({ error: null });
    const user = userEvent.setup();
    render(<SignupForm inviteToken="tok-abc" />);

    await user.type(email(), "a@b.com");
    await user.type(password(), "correcthorse");
    await user.type(confirmPassword(), "wronghorse");
    await user.click(screen.getByRole("button", { name: /create account/i }));
    await screen.findByText("Passwords do not match.");

    await user.clear(confirmPassword());
    await user.type(confirmPassword(), "correcthorse");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => expect(signup).toHaveBeenCalled());
    expect(screen.queryByText("Passwords do not match.")).not.toBeInTheDocument();
  });

  it("shows the error message returned by the action (e.g. an expired invite)", async () => {
    signup.mockResolvedValue({ error: "This invite is no longer valid." });
    const user = userEvent.setup();
    render(<SignupForm inviteToken="tok-abc" />);

    await fillForm(user, "correcthorse");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    expect(await screen.findByText("This invite is no longer valid.")).toBeInTheDocument();
  });

  it("marks the session as pending on submit, same signal as login (post-signup redirect to /holdings)", async () => {
    signup.mockResolvedValue({ error: null });
    const user = userEvent.setup();
    render(<SignupForm inviteToken="tok-abc" />);

    await fillForm(user, "correcthorse");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    expect(markPendingLogin).toHaveBeenCalled();
  });

  it("clears the pending-login signal when the action returns an error", async () => {
    signup.mockResolvedValue({ error: "This invite is no longer valid." });
    const user = userEvent.setup();
    render(<SignupForm inviteToken="tok-abc" />);

    await fillForm(user, "correcthorse");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    await screen.findByText("This invite is no longer valid.");
    expect(clearPendingLogin).toHaveBeenCalled();
  });

  it("clears the pending-login signal when the action throws, not only when it returns { error }", async () => {
    signup.mockRejectedValue(new Error("backend unreachable"));
    const user = userEvent.setup();
    render(<SignupForm inviteToken="tok-abc" />);

    await fillForm(user, "correcthorse");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => expect(clearPendingLogin).toHaveBeenCalled());
  });
});
