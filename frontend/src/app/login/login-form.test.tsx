import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { login, markPendingLogin, clearPendingLogin } = vi.hoisted(() => ({
  login: vi.fn(),
  markPendingLogin: vi.fn(),
  clearPendingLogin: vi.fn(),
}));

vi.mock("./actions", () => ({ login }));
vi.mock("@/hooks/use-session", () => ({ markPendingLogin, clearPendingLogin }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { catalogs } from "@/locales";
import { LoginForm } from "./login-form";

function renderForm() {
  return render(
    <LocaleProvider>
      <LoginForm />
    </LocaleProvider>,
  );
}

describe("LoginForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("submits the entered email and password to the login action", async () => {
    login.mockResolvedValue({ error: null });
    const user = userEvent.setup();
    renderForm();

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
    renderForm();

    await user.type(screen.getByLabelText(/email/i), "a@b.com");
    await user.type(screen.getByLabelText(/password/i), "correcthorse");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    expect(markPendingLogin).toHaveBeenCalled();
  });

  it("shows the error message returned by the action", async () => {
    login.mockResolvedValue({ error: "Invalid email or password." });
    const user = userEvent.setup();
    renderForm();

    await user.type(screen.getByLabelText(/email/i), "a@b.com");
    await user.type(screen.getByLabelText(/password/i), "wrong");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    expect(await screen.findByText("Invalid email or password.")).toBeInTheDocument();
  });

  it("clears the pending-login signal when the action returns an error", async () => {
    login.mockResolvedValue({ error: "Invalid email or password." });
    const user = userEvent.setup();
    renderForm();

    await user.type(screen.getByLabelText(/email/i), "a@b.com");
    await user.type(screen.getByLabelText(/password/i), "wrong");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    await screen.findByText("Invalid email or password.");
    expect(clearPendingLogin).toHaveBeenCalled();
  });

  it("clears the pending-login signal when the action throws, not only when it returns { error }", async () => {
    login.mockRejectedValue(new Error("auth.portfonia.com unreachable"));
    const user = userEvent.setup();
    renderForm();

    await user.type(screen.getByLabelText(/email/i), "a@b.com");
    await user.type(screen.getByLabelText(/password/i), "wrong");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    await waitFor(() => expect(clearPendingLogin).toHaveBeenCalled());
  });

  it("links an interested visitor to the public waitlist", () => {
    renderForm();

    expect(screen.getByText(/internal testing/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /join the waitlist/i })).toHaveAttribute("href", "/waitlist");
    expect(screen.queryByRole("link", { name: /sign ?up/i })).not.toBeInTheDocument();
  });

  it("joins the waitlist hint and link with one space in English", () => {
    renderForm();

    const link = screen.getByRole("link", { name: /join the waitlist/i });
    expect(link.parentElement?.textContent).toBe(
      "Portfonia is in internal testing. If you're interested, you can join the waitlist",
    );
  });

  it("joins the waitlist hint and link with no space in Chinese", async () => {
    const original = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      value: { getItem: () => "zh-Hans", setItem: () => undefined },
      configurable: true,
    });
    try {
      renderForm();

      const { waitlistHint, waitlistLink } = catalogs["zh-Hans"].auth;
      const link = await screen.findByRole("link", { name: waitlistLink });
      expect(waitlistHint.endsWith(" ")).toBe(false);
      expect(link.parentElement?.textContent).toBe(`${waitlistHint}${waitlistLink}`);
    } finally {
      if (original) Object.defineProperty(window, "localStorage", original);
    }
  });

});
