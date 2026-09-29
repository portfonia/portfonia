import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

type PaddleEvent = { name?: string; data?: { transaction_id?: string } };

const {
  getCheckoutConfig,
  getPurchaseStatus,
  initializePaddle,
  pricePreview,
  checkoutOpen,
  checkoutClose,
  routerRefresh,
} = vi.hoisted(() => ({
  getCheckoutConfig: vi.fn(),
  getPurchaseStatus: vi.fn(),
  initializePaddle: vi.fn(),
  pricePreview: vi.fn(),
  checkoutOpen: vi.fn(),
  checkoutClose: vi.fn(),
  routerRefresh: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ getCheckoutConfig, getPurchaseStatus }));
vi.mock("@paddle/paddle-js", () => ({ initializePaddle }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: routerRefresh }) }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { CreditPurchase } from "./credit-purchase";

let onEvent: ((event: PaddleEvent) => void) | undefined;

beforeEach(() => {
  vi.resetAllMocks();
  onEvent = undefined;
  getCheckoutConfig.mockResolvedValue({
    environment: "production", client_token: "live_test", user_id: "user-1",
    email: "buyer@example.com", packs: [
      { price_id: "pri_test", credits: "10.00" },
      { price_id: "pri_20", credits: "20.00" },
    ],
  });
  initializePaddle.mockImplementation((options: { eventCallback?: (event: PaddleEvent) => void }) => {
    onEvent = options.eventCallback;
    return Promise.resolve({ PricePreview: pricePreview, Checkout: { open: checkoutOpen, close: checkoutClose } });
  });
  pricePreview.mockResolvedValue({ data: { details: { lineItems: [
    { price: { id: "pri_test" }, formattedTotals: { total: "HK$78.00" } },
    { price: { id: "pri_20" }, formattedTotals: { total: "HK$156.00" } },
  ] } } });
});

async function renderReady() {
  render(<LocaleProvider><CreditPurchase /></LocaleProvider>);
  expect(await screen.findByText("HK$78.00")).toBeInTheDocument();
}

it("renders Paddle's price and opens the selected pack with account identity", async () => {
  await renderReady();
  await userEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  expect(checkoutOpen).toHaveBeenCalledWith({
    items: [{ priceId: "pri_test", quantity: 1 }], customer: { email: "buyer@example.com" },
    customData: { user_id: "user-1" },
    settings: { displayMode: "overlay", variant: "one-page" },
  });
  expect(screen.getByRole("link", { name: "Terms of Service" })).toHaveAttribute("href", "/terms");
  expect(screen.getByRole("link", { name: "Refund Policy" })).toHaveAttribute("href", "/refund");
});

it("disables every Buy button while opening and ignores a second click", async () => {
  await renderReady();
  const [first, second] = screen.getAllByRole("button", { name: "Buy" });
  fireEvent.click(first);
  expect(screen.getByRole("button", { name: "Opening checkout…" })).toBeDisabled();
  expect(second).toBeDisabled();
  fireEvent.click(first);
  fireEvent.click(second);
  expect(checkoutOpen).toHaveBeenCalledTimes(1);
});

it("re-enables on loaded or closed, and shows open-failed on error or a 15s timeout", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    await renderReady();
    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    act(() => { onEvent?.({ name: "checkout.loaded" }); });
    expect(screen.getAllByRole("button", { name: "Buy" })[0]).toBeEnabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    act(() => { onEvent?.({ name: "checkout.closed" }); });
    expect(screen.getAllByRole("button", { name: "Buy" })[0]).toBeEnabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    act(() => { onEvent?.({ name: "checkout.error" }); });
    expect(screen.getAllByRole("button", { name: "Buy" })[0]).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent("Checkout did not open. Please try again.");

    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(screen.getAllByRole("button", { name: "Buy" })[0]).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent("Checkout did not open. Please try again.");
  } finally {
    vi.useRealTimers();
  }
});

it("polls the purchase after checkout.completed and refreshes once it is credited", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    getPurchaseStatus.mockResolvedValue({ transaction_id: "txn_A", credited: false, credits: null });
    await renderReady();
    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    act(() => { onEvent?.({ name: "checkout.completed", data: { transaction_id: "txn_A" } }); });
    expect(checkoutClose).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Payment received. Adding your credits; this usually takes under a minute.",
    );
    await act(async () => { await Promise.resolve(); });
    expect(getPurchaseStatus).toHaveBeenCalledWith("txn_A");
    getPurchaseStatus.mockResolvedValue({ transaction_id: "txn_A", credited: true, credits: "10.00" });
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(screen.getByRole("status")).toHaveTextContent("10 credits added to your balance.");
    expect(routerRefresh).toHaveBeenCalledTimes(1);
    const calls = getPurchaseStatus.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(9_000); });
    expect(getPurchaseStatus).toHaveBeenCalledTimes(calls);
  } finally {
    vi.useRealTimers();
  }
});

it("does not start another purchase-status request while one is in flight", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    let resolvePending: (value: { transaction_id: string; credited: boolean; credits: null }) => void = () => {};
    const pending = new Promise<{ transaction_id: string; credited: boolean; credits: null }>((resolve) => {
      resolvePending = resolve;
    });
    getPurchaseStatus.mockResolvedValue({ transaction_id: "txn_A", credited: false, credits: null });
    getPurchaseStatus.mockReturnValueOnce(pending);
    await renderReady();
    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    act(() => { onEvent?.({ name: "checkout.completed", data: { transaction_id: "txn_A" } }); });
    expect(getPurchaseStatus).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(9_000); });
    expect(getPurchaseStatus).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolvePending({ transaction_id: "txn_A", credited: false, credits: null });
      await Promise.resolve();
    });
    expect(getPurchaseStatus).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(2_999); });
    expect(getPurchaseStatus).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(getPurchaseStatus).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
  }
});

it("stops polling after 120 seconds and shows the delayed notice", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    getPurchaseStatus.mockResolvedValue({ transaction_id: "txn_A", credited: false, credits: null });
    await renderReady();
    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    act(() => { onEvent?.({ name: "checkout.completed", data: { transaction_id: "txn_A" } }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(119_000); });
    expect(screen.getByRole("status")).toHaveTextContent(
      "Payment received. Adding your credits; this usually takes under a minute.",
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(screen.getByRole("status")).toHaveTextContent("contact info@portfonia.com");
    const calls = getPurchaseStatus.mock.calls.length;
    expect(calls).toBeGreaterThan(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(9_000); });
    expect(getPurchaseStatus).toHaveBeenCalledTimes(calls);
  } finally {
    vi.useRealTimers();
  }
});

it("ignores checkout.completed after the component unmounts", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const view = render(<LocaleProvider><CreditPurchase /></LocaleProvider>);
    expect(await screen.findByText("HK$78.00")).toBeInTheDocument();
    view.unmount();
    act(() => { onEvent?.({ name: "checkout.completed", data: { transaction_id: "txn_A" } }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    expect(getPurchaseStatus).not.toHaveBeenCalled();
  } finally {
    vi.useRealTimers();
  }
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
