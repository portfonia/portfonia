import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

type PaddleEvent = {
  name?: string;
  data?: { transaction_id?: string; items?: { price_id: string }[] };
};

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

it("still handles checkout events when an earlier Paddle init finishes last", async () => {
  const config = {
    environment: "production", client_token: "live_test", user_id: "user-1",
    email: "buyer@example.com", packs: [
      { price_id: "pri_test", credits: "10.00" },
      { price_id: "pri_20", credits: "20.00" },
    ],
  };
  let releaseFirstConfig: (value: typeof config) => void = () => {};
  const firstConfig = new Promise<typeof config>((resolve) => {
    releaseFirstConfig = resolve;
  });
  getCheckoutConfig.mockReset();
  getCheckoutConfig.mockResolvedValue(config);
  getCheckoutConfig.mockReturnValueOnce(firstConfig);
  const callbacks: Array<((event: PaddleEvent) => void) | undefined> = [];
  initializePaddle.mockImplementation((options: { eventCallback?: (event: PaddleEvent) => void }) => {
    callbacks.push(options.eventCallback);
    onEvent = callbacks[callbacks.length - 1];
    return Promise.resolve({ PricePreview: pricePreview, Checkout: { open: checkoutOpen, close: checkoutClose } });
  });

  const first = render(<LocaleProvider><CreditPurchase /></LocaleProvider>);
  await act(async () => { await Promise.resolve(); });
  first.unmount();

  render(<LocaleProvider><CreditPurchase /></LocaleProvider>);
  expect(await screen.findByText("HK$78.00")).toBeInTheDocument();
  expect(initializePaddle).toHaveBeenCalledTimes(1);

  await act(async () => { releaseFirstConfig(config); });
  expect(initializePaddle).toHaveBeenCalledTimes(2);
  expect(callbacks).toHaveLength(2);
  expect(onEvent).toBe(callbacks[1]);

  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  expect(screen.getByRole("button", { name: "Opening checkout…" })).toBeDisabled();
  act(() => { onEvent?.({ name: "checkout.loaded" }); });
  expect(screen.getAllByRole("button", { name: "Buy" })[0]).toBeEnabled();

  act(() => { onEvent?.({ name: "checkout.completed", data: { transaction_id: "txn_A" } }); });
  await act(async () => { await Promise.resolve(); });
  expect(getPurchaseStatus).toHaveBeenCalledWith("txn_A");
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

it("reopens the same unfinished transaction when Buy is clicked again for the same pack", async () => {
  await renderReady();
  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  act(() => { onEvent?.({ name: "checkout.loaded", data: { transaction_id: "txn_a", items: [{ price_id: "pri_test" }] } }); });
  act(() => { onEvent?.({ name: "checkout.closed" }); });
  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  expect(checkoutOpen).toHaveBeenCalledTimes(2);
  expect(checkoutOpen).toHaveBeenNthCalledWith(1, {
    items: [{ priceId: "pri_test", quantity: 1 }],
    customer: { email: "buyer@example.com" },
    customData: { user_id: "user-1" },
    settings: { displayMode: "overlay", variant: "one-page" },
  });
  expect(checkoutOpen).toHaveBeenNthCalledWith(2, {
    transactionId: "txn_a",
    settings: { displayMode: "overlay", variant: "one-page" },
  });
});

it("does not reuse a transaction when Buy is clicked for a different pack", async () => {
  await renderReady();
  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  act(() => { onEvent?.({ name: "checkout.loaded", data: { transaction_id: "txn_a", items: [{ price_id: "pri_test" }] } }); });
  act(() => { onEvent?.({ name: "checkout.closed" }); });
  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[1]);
  expect(checkoutOpen).toHaveBeenCalledTimes(2);
  expect(checkoutOpen).toHaveBeenNthCalledWith(1, {
    items: [{ priceId: "pri_test", quantity: 1 }],
    customer: { email: "buyer@example.com" },
    customData: { user_id: "user-1" },
    settings: { displayMode: "overlay", variant: "one-page" },
  });
  expect(checkoutOpen).toHaveBeenNthCalledWith(2, {
    items: [{ priceId: "pri_20", quantity: 1 }],
    customer: { email: "buyer@example.com" },
    customData: { user_id: "user-1" },
    settings: { displayMode: "overlay", variant: "one-page" },
  });
});

it("starts a new transaction after checkout.completed, even for the same pack", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    getPurchaseStatus.mockResolvedValue({ transaction_id: "txn_a", credited: false, credits: null });
    await renderReady();
    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    act(() => { onEvent?.({ name: "checkout.loaded", data: { transaction_id: "txn_a", items: [{ price_id: "pri_test" }] } }); });
    act(() => { onEvent?.({ name: "checkout.completed", data: { transaction_id: "txn_a" } }); });
    await act(async () => { await Promise.resolve(); });
    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    expect(checkoutOpen).toHaveBeenCalledTimes(2);
    expect(checkoutOpen).toHaveBeenNthCalledWith(1, {
      items: [{ priceId: "pri_test", quantity: 1 }],
      customer: { email: "buyer@example.com" },
      customData: { user_id: "user-1" },
      settings: { displayMode: "overlay", variant: "one-page" },
    });
    expect(checkoutOpen).toHaveBeenNthCalledWith(2, {
      items: [{ priceId: "pri_test", quantity: 1 }],
      customer: { email: "buyer@example.com" },
      customData: { user_id: "user-1" },
      settings: { displayMode: "overlay", variant: "one-page" },
    });
  } finally {
    vi.useRealTimers();
  }
});

it("falls back to a new transaction after checkout.error on a reopen", async () => {
  await renderReady();
  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  act(() => { onEvent?.({ name: "checkout.loaded", data: { transaction_id: "txn_a", items: [{ price_id: "pri_test" }] } }); });
  act(() => { onEvent?.({ name: "checkout.closed" }); });
  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  act(() => { onEvent?.({ name: "checkout.error" }); });
  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  expect(checkoutOpen).toHaveBeenCalledTimes(3);
  expect(checkoutOpen).toHaveBeenNthCalledWith(1, {
    items: [{ priceId: "pri_test", quantity: 1 }],
    customer: { email: "buyer@example.com" },
    customData: { user_id: "user-1" },
    settings: { displayMode: "overlay", variant: "one-page" },
  });
  expect(checkoutOpen).toHaveBeenNthCalledWith(2, {
    transactionId: "txn_a",
    settings: { displayMode: "overlay", variant: "one-page" },
  });
  expect(checkoutOpen).toHaveBeenNthCalledWith(3, {
    items: [{ priceId: "pri_test", quantity: 1 }],
    customer: { email: "buyer@example.com" },
    customData: { user_id: "user-1" },
    settings: { displayMode: "overlay", variant: "one-page" },
  });
});

it("falls back to a new transaction after the reopen's 15s open timeout", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    await renderReady();
    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    act(() => { onEvent?.({ name: "checkout.loaded", data: { transaction_id: "txn_a", items: [{ price_id: "pri_test" }] } }); });
    act(() => { onEvent?.({ name: "checkout.closed" }); });
    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    expect(checkoutOpen).toHaveBeenCalledTimes(3);
    expect(checkoutOpen).toHaveBeenNthCalledWith(1, {
      items: [{ priceId: "pri_test", quantity: 1 }],
      customer: { email: "buyer@example.com" },
      customData: { user_id: "user-1" },
      settings: { displayMode: "overlay", variant: "one-page" },
    });
    expect(checkoutOpen).toHaveBeenNthCalledWith(2, {
      transactionId: "txn_a",
      settings: { displayMode: "overlay", variant: "one-page" },
    });
    expect(checkoutOpen).toHaveBeenNthCalledWith(3, {
      items: [{ priceId: "pri_test", quantity: 1 }],
      customer: { email: "buyer@example.com" },
      customData: { user_id: "user-1" },
      settings: { displayMode: "overlay", variant: "one-page" },
    });
  } finally {
    vi.useRealTimers();
  }
});

it("does not remember a transaction when checkout.loaded carries no transaction_id", async () => {
  await renderReady();
  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  act(() => { onEvent?.({ name: "checkout.loaded" }); });
  act(() => { onEvent?.({ name: "checkout.closed" }); });
  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  expect(checkoutOpen).toHaveBeenCalledTimes(2);
  expect(checkoutOpen).toHaveBeenNthCalledWith(1, {
    items: [{ priceId: "pri_test", quantity: 1 }],
    customer: { email: "buyer@example.com" },
    customData: { user_id: "user-1" },
    settings: { displayMode: "overlay", variant: "one-page" },
  });
  expect(checkoutOpen).toHaveBeenNthCalledWith(2, {
    items: [{ priceId: "pri_test", quantity: 1 }],
    customer: { email: "buyer@example.com" },
    customData: { user_id: "user-1" },
    settings: { displayMode: "overlay", variant: "one-page" },
  });
});

it("pairs a late checkout.loaded with its own pack, not the pack clicked last", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    await renderReady();
    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[1]);
    act(() => { onEvent?.({ name: "checkout.loaded", data: { transaction_id: "txn_b", items: [{ price_id: "pri_20" }] } }); });
    act(() => { onEvent?.({ name: "checkout.loaded", data: { transaction_id: "txn_a", items: [{ price_id: "pri_test" }] } }); });
    act(() => { onEvent?.({ name: "checkout.closed" }); });
    fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[1]);
    expect(checkoutOpen).toHaveBeenCalledTimes(3);
    expect(checkoutOpen).toHaveBeenNthCalledWith(3, {
      items: [{ priceId: "pri_20", quantity: 1 }],
      customer: { email: "buyer@example.com" },
      customData: { user_id: "user-1" },
      settings: { displayMode: "overlay", variant: "one-page" },
    });
  } finally {
    vi.useRealTimers();
  }
});

afterEach(() => {
  vi.restoreAllMocks();
});

// Keep history effects isolated from jsdom's asynchronous navigation.
function spyCheckoutHistory() {
  return {
    pushState: vi.spyOn(window.history, "pushState").mockImplementation(() => {}),
    back: vi.spyOn(window.history, "back").mockImplementation(() => {}),
  };
}

async function openLoadedCheckout() {
  await renderReady();
  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  act(() => {
    onEvent?.({ name: "checkout.loaded", data: { transaction_id: "txn_a", items: [{ price_id: "pri_test" }] } });
  });
}

it("adds one same-URL history entry when checkout loads", async () => {
  const { pushState } = spyCheckoutHistory();
  await openLoadedCheckout();
  expect(pushState).toHaveBeenCalledExactlyOnceWith(null, "", window.location.href);
});

it.each([false, true])("closes checkout on Back without navigating again (closed event: %s)", async (emitsClosed) => {
  const { back } = spyCheckoutHistory();
  await openLoadedCheckout();
  if (emitsClosed) checkoutClose.mockImplementation(() => { onEvent?.({ name: "checkout.closed" }); });
  act(() => { window.dispatchEvent(new PopStateEvent("popstate")); });
  expect(checkoutClose).toHaveBeenCalledTimes(1);
  expect(back).not.toHaveBeenCalled();
  act(() => { window.dispatchEvent(new PopStateEvent("popstate")); });
  expect(checkoutClose).toHaveBeenCalledTimes(1);
});

it("reuses the remembered transaction after Back closes checkout", async () => {
  spyCheckoutHistory();
  await openLoadedCheckout();
  act(() => { window.dispatchEvent(new PopStateEvent("popstate")); });
  expect(checkoutClose).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  expect(checkoutOpen).toHaveBeenNthCalledWith(2, {
    transactionId: "txn_a",
    settings: { displayMode: "overlay", variant: "one-page" },
  });
  expect(checkoutOpen.mock.calls[1][0]).not.toHaveProperty("items");
});

it("removes the entry on Paddle close and ignores the resulting popstate", async () => {
  const { pushState, back } = spyCheckoutHistory();
  await openLoadedCheckout();
  act(() => { onEvent?.({ name: "checkout.closed" }); });
  expect(back).toHaveBeenCalledTimes(1);
  act(() => { window.dispatchEvent(new PopStateEvent("popstate")); });
  expect(checkoutClose).not.toHaveBeenCalled();
  // Another open/close cycle adds and removes exactly one more entry.
  fireEvent.click(screen.getAllByRole("button", { name: "Buy" })[0]);
  act(() => { onEvent?.({ name: "checkout.loaded" }); });
  act(() => { onEvent?.({ name: "checkout.closed" }); });
  expect(pushState).toHaveBeenCalledTimes(2);
  expect(back).toHaveBeenCalledTimes(2);
  act(() => { window.dispatchEvent(new PopStateEvent("popstate")); });
  expect(checkoutClose).not.toHaveBeenCalled();
});

it.each([false, true])("removes the entry only once on completion then close (synchronous closed event: %s)", async (emitsClosed) => {
  const { back } = spyCheckoutHistory();
  getPurchaseStatus.mockResolvedValue({ transaction_id: "txn_a", credited: false, credits: null });
  await openLoadedCheckout();
  if (emitsClosed) checkoutClose.mockImplementation(() => { onEvent?.({ name: "checkout.closed" }); });
  act(() => { onEvent?.({ name: "checkout.completed", data: { transaction_id: "txn_a" } }); });
  expect(back).toHaveBeenCalledTimes(1);
  expect(checkoutClose).toHaveBeenCalledTimes(1);
  act(() => { onEvent?.({ name: "checkout.closed" }); });
  act(() => { window.dispatchEvent(new PopStateEvent("popstate")); });
  expect(back).toHaveBeenCalledTimes(1);
  expect(checkoutClose).toHaveBeenCalledTimes(1);
});

it("does not add a second history entry for a repeated loaded event", async () => {
  const { pushState } = spyCheckoutHistory();
  await openLoadedCheckout();
  act(() => { onEvent?.({ name: "checkout.loaded" }); });
  expect(pushState).toHaveBeenCalledTimes(1);
});

it("ignores popstate before any checkout was opened", async () => {
  const { pushState, back } = spyCheckoutHistory();
  await renderReady();
  act(() => { window.dispatchEvent(new PopStateEvent("popstate")); });
  expect(checkoutClose).not.toHaveBeenCalled();
  expect(back).not.toHaveBeenCalled();
  expect(pushState).not.toHaveBeenCalled();
});
