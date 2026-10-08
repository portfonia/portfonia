import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { submitWaitlist } = vi.hoisted(() => ({ submitWaitlist: vi.fn() }));
vi.mock("./actions", () => ({ submitWaitlist }));
vi.mock("next/script", () => ({ default: () => null }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { WaitlistForm } from "./waitlist-form";

function renderForm() {
  return render(
    <LocaleProvider routeLocale={null}>
      <WaitlistForm />
    </LocaleProvider>,
  );
}

describe("WaitlistForm", () => {
  beforeEach(() => vi.clearAllMocks());

  it("submits email and current locale", async () => {
    submitWaitlist.mockResolvedValue({ received: true, error: null });
    const user = userEvent.setup();
    renderForm();
    await user.type(screen.getByLabelText(/^email$/i), "a@x.com");
    await user.click(screen.getByRole("button", { name: /join waitlist/i }));
    await waitFor(() => expect(submitWaitlist).toHaveBeenCalled());
    const data = submitWaitlist.mock.calls[0][1] as FormData;
    expect(data.get("email")).toBe("a@x.com");
    expect(data.get("locale")).toBe("en");
    expect(await screen.findByRole("status")).toHaveTextContent(/request has been received/i);
  });

  it("shows an error while keeping the form", async () => {
    submitWaitlist.mockResolvedValue({ error: "Please complete verification." });
    const user = userEvent.setup();
    renderForm();
    await user.type(screen.getByLabelText(/^email$/i), "a@x.com");
    await user.click(screen.getByRole("button", { name: /join waitlist/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Please complete verification.");
    expect(screen.getByLabelText(/^email$/i)).toBeInTheDocument();
  });
});
