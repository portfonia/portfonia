import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

const { getCheckoutConfig, initializePaddle, pricePreview, checkoutOpen } = vi.hoisted(() => ({
  getCheckoutConfig: vi.fn(),
  initializePaddle: vi.fn(),
  pricePreview: vi.fn(),
  checkoutOpen: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ getCheckoutConfig }));
vi.mock("@paddle/paddle-js", () => ({ initializePaddle }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { CreditPurchase } from "./credit-purchase";

beforeEach(() => {
  vi.resetAllMocks();
  getCheckoutConfig.mockResolvedValue({
    environment: "production", client_token: "live_test", user_id: "user-1",
    email: "buyer@example.com", packs: [{ price_id: "pri_test", credits: "10.00" }],
  });
  initializePaddle.mockResolvedValue({ PricePreview: pricePreview, Checkout: { open: checkoutOpen } });
  pricePreview.mockResolvedValue({ data: { details: { lineItems: [
    { price: { id: "pri_test" }, formattedTotals: { total: "HK$78.00" } },
  ] } } });
});

it("renders Paddle's price and opens the selected pack with account identity", async () => {
  render(<LocaleProvider><CreditPurchase /></LocaleProvider>);
  expect(await screen.findByText("HK$78.00")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Buy" }));
  expect(checkoutOpen).toHaveBeenCalledWith(expect.objectContaining({
    items: [{ priceId: "pri_test", quantity: 1 }], customer: { email: "buyer@example.com" },
    customData: { user_id: "user-1" },
    settings: expect.objectContaining({ displayMode: "overlay", variant: "one-page",
      successUrl: expect.stringMatching(/\/profile\?purchase=completed$/) }),
  }));
  expect(screen.getByRole("link", { name: "Terms of Service" })).toHaveAttribute("href", "/terms");
  expect(screen.getByRole("link", { name: "Refund Policy" })).toHaveAttribute("href", "/refund");
});

it("shows unavailable when config is missing or preview fails", async () => {
  getCheckoutConfig.mockResolvedValueOnce(null);
  const first = render(<LocaleProvider><CreditPurchase /></LocaleProvider>);
  expect(await screen.findByText("Purchasing is temporarily unavailable.")).toBeInTheDocument();
  first.unmount();
  pricePreview.mockRejectedValueOnce(new Error("preview failed"));
  render(<LocaleProvider><CreditPurchase /></LocaleProvider>);
  await waitFor(() => expect(screen.getByText("Purchasing is temporarily unavailable.")).toBeInTheDocument());
});
