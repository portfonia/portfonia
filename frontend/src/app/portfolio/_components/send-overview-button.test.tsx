import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { sendPortfolioOverview } = vi.hoisted(() => ({
  sendPortfolioOverview: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, sendPortfolioOverview };
});
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { SendOverviewButton } from "./send-overview-button";

beforeEach(() => {
  sendPortfolioOverview.mockReset();
});

describe("SendOverviewButton", () => {
  it("shows a success message when the send is dispatched", async () => {
    sendPortfolioOverview.mockResolvedValue({ sent: true, retry_after_seconds: null });
    const user = userEvent.setup();
    render(
      <LocaleProvider>
        <SendOverviewButton baseCurrency="USD" />
      </LocaleProvider>,
    );

    await user.click(screen.getByRole("button", { name: /send holdings overview/i }));

    await waitFor(() => expect(screen.getByText(/sent to your email/i)).toBeInTheDocument());
    expect(sendPortfolioOverview).toHaveBeenCalledWith("USD");
  });

  it("shows remaining cooldown time instead of an error when still in cooldown", async () => {
    sendPortfolioOverview.mockResolvedValue({ sent: false, retry_after_seconds: 610 });
    const user = userEvent.setup();
    render(
      <LocaleProvider>
        <SendOverviewButton baseCurrency="USD" />
      </LocaleProvider>,
    );

    await user.click(screen.getByRole("button", { name: /send holdings overview/i }));

    // 610s rounds up to 11 minutes — never claim "0 min" for a real cooldown.
    await waitFor(() => expect(screen.getByText(/11 min/)).toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows an error message on network failure", async () => {
    sendPortfolioOverview.mockRejectedValue(new Error("network down"));
    const user = userEvent.setup();
    render(
      <LocaleProvider>
        <SendOverviewButton baseCurrency="USD" />
      </LocaleProvider>,
    );

    await user.click(screen.getByRole("button", { name: /send holdings overview/i }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });

  it("is disabled while a currency switch is in flight (review 5100733033)", () => {
    render(
      <LocaleProvider>
        <SendOverviewButton baseCurrency="USD" disabled />
      </LocaleProvider>,
    );

    expect(screen.getByRole("button", { name: /send holdings overview/i })).toBeDisabled();
  });
});
