import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { confirmUnsubscribe } = vi.hoisted(() => ({
  confirmUnsubscribe: vi.fn(),
}));

vi.mock("./actions", () => ({ confirmUnsubscribe }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { UnsubscribeStatus } from "./page";
import { UnsubscribeForm } from "./unsubscribe-form";

function renderForm(status: UnsubscribeStatus, token = "tok-1") {
  return render(
    <LocaleProvider>
      <UnsubscribeForm token={token} status={status} />
    </LocaleProvider>,
  );
}

describe("UnsubscribeForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows the invalid/expired message and no form when the token isn't found", () => {
    renderForm({ found: false, email: null });

    expect(screen.getByRole("alert")).toHaveTextContent(/invalid or has expired/i);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders the confirm form for a valid token, submitting the token", async () => {
    confirmUnsubscribe.mockResolvedValue({ error: null, email: "a@b.com" });
    const user = userEvent.setup();
    renderForm({ found: true, email: "a@b.com" }, "tok-1");

    await user.click(screen.getByRole("button", { name: /confirm unsubscribe/i }));

    await waitFor(() => expect(confirmUnsubscribe).toHaveBeenCalled());
    const submittedForm = confirmUnsubscribe.mock.calls[0][1] as FormData;
    expect(submittedForm.get("token")).toBe("tok-1");
  });

  it("shows the success message and profile link once the backend confirms", async () => {
    confirmUnsubscribe.mockResolvedValue({ error: null, email: "a@b.com" });
    const user = userEvent.setup();
    renderForm({ found: true, email: "a@b.com" });

    await user.click(screen.getByRole("button", { name: /confirm unsubscribe/i }));

    expect(await screen.findByRole("status")).toHaveTextContent("a@b.com");
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/profile");
  });

  it("shows a translated error and keeps the form when the action rejects", async () => {
    confirmUnsubscribe.mockResolvedValue({ error: "invalidOrExpired" });
    const user = userEvent.setup();
    renderForm({ found: true, email: "a@b.com" });

    await user.click(screen.getByRole("button", { name: /confirm unsubscribe/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/invalid or has expired/i);
    expect(screen.getByRole("button", { name: /confirm unsubscribe/i })).toBeInTheDocument();
  });

  it("uses one consistent unsubscribe register across the whole page (issue #289 item 2)", async () => {
    // The heading/button chrome and the body copy must not mix "unsubscribe"
    // with "revoke verification" — plain unsubscribe language everywhere,
    // generic to the platform (not Portfonia-report-specific, so Vigil's
    // future reuse of the same page shape needs no rewrite).
    confirmUnsubscribe.mockResolvedValue({ error: null, email: "a@b.com" });
    const user = userEvent.setup();
    renderForm({ found: true, email: "a@b.com" });

    expect(screen.getByRole("heading", { name: /^unsubscribe$/i })).toBeInTheDocument();
    expect(
      screen.getByText(/this address will stop receiving reports from this platform until it is verified again/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/revoke verification/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /confirm unsubscribe/i }));

    expect(await screen.findByRole("status")).toHaveTextContent(/you have unsubscribed a@b\.com/i);
    expect(screen.queryByText(/revoke verification/i)).not.toBeInTheDocument();
  });
});
