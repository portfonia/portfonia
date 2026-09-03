import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getPortfolioSummary } = vi.hoisted(() => ({
  getPortfolioSummary: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, getPortfolioSummary };
});
// Same reasoning as holdings-manager.test.tsx: lib/api.ts's real
// (importActual'd) exports pull in logout() from auth-actions.ts, which
// pulls in the server-only-guarded Supabase server client.
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { HoldingValueOut, PortfolioSummary } from "@/lib/api";
import { PortfolioPageBody } from "./portfolio-page-body";

function priced(overrides: Partial<HoldingValueOut>): HoldingValueOut {
  return {
    holding_id: "h1",
    name: "Apple",
    ticker: "AAPL",
    fund_code: null,
    currency: "USD",
    asset_type: "stock",
    asset_class: "STOCK",
    sector: "Technology",
    market: "US",
    market_value: "3000.00",
    market_value_base: "3000.00",
    price_as_of: null,
    pricing_mode: "auto",
    capture_supported: true,
    broker: "Fidelity",
    account: null,
    portfolio: "Retirement",
    avg_cost: "250",
    shares: "10",
    notes: null,
    cost_basis_base: "2500.00",
    unrealized_pnl_base: "500.00",
    unrealized_pnl_pct: "0.2000",
    ...overrides,
  };
}

function summary(overrides: Partial<PortfolioSummary>): PortfolioSummary {
  return {
    base_currency: "USD",
    fx_date: "2026-01-02",
    total_base: "3000.00",
    by_market: { US: "3000.00" },
    by_currency: { USD: "3000.00" },
    by_asset_type: { stock: "3000.00" },
    by_sector: { Technology: "3000.00" },
    by_asset_class: { STOCK: "3000.00" },
    by_group: { Retirement: "3000.00" },
    by_broker: { Fidelity: "3000.00" },
    by_account: { Other: "3000.00" },
    total_cost_basis_base: "2500.00",
    total_unrealized_pnl_base: "500.00",
    total_unrealized_pnl_pct: "0.2000",
    price_as_of_date: "2026-01-02",
    stale_tickers: [],
    holdings: [priced({})],
    ...overrides,
  };
}

function renderBody(initialSummary: PortfolioSummary | null, initialLoadError = false) {
  return render(
    <LocaleProvider>
      <PortfolioPageBody initialSummary={initialSummary} initialLoadError={initialLoadError} />
    </LocaleProvider>,
  );
}

// Several sections legitimately repeat the same formatted amount for this
// fixture (the single holding's market value, the market-breakdown legend,
// the total assets figure) — scope to the "Total assets" card specifically.
function totalAssetsValue(): HTMLElement {
  const card = screen.getByText("Total assets").closest('[data-slot="card"]');
  if (!card) throw new Error("Total assets card not found");
  return within(card as HTMLElement).getByText(/USD|CNY/);
}

beforeEach(() => {
  getPortfolioSummary.mockReset();
});

describe("PortfolioPageBody", () => {
  it("shows a load error when there is no initial summary", () => {
    renderBody(null, true);
    expect(screen.getByText("Couldn't load your portfolio. Try refreshing the page.")).toBeInTheDocument();
  });

  it("renders the total assets figure from the initial server-loaded summary", () => {
    renderBody(summary({}));
    expect(totalAssetsValue()).toHaveTextContent("3,000.00 USD");
  });

  it("splits capture-unsupported holdings into the no-live-price section, not the main table", () => {
    const unsupported = priced({
      holding_id: "h2",
      name: "Unresolvable",
      ticker: null,
      market: "Other",
      capture_supported: false,
      market_value_base: null,
      cost_basis_base: null,
      unrealized_pnl_base: null,
      unrealized_pnl_pct: null,
      notes: "Private placement, no public ticker",
    });
    renderBody(summary({ holdings: [priced({}), unsupported] }));

    expect(screen.getByText("No market quote")).toBeInTheDocument();
    expect(screen.getByText("Unresolvable")).toBeInTheDocument();
    // Grok review round 2 (PR #322): notes was added to HoldingValueOut and
    // must render in the no-quote block per decision 3 / issue comment 2.
    expect(screen.getByText("Private placement, no public ticker")).toBeInTheDocument();
  });

  it("refetches the summary when the currency switcher changes", async () => {
    const user = userEvent.setup();
    getPortfolioSummary.mockResolvedValue(
      summary({
        base_currency: "CNY",
        total_base: "21000.00",
        by_market: { US: "21000.00" },
      }),
    );
    renderBody(summary({}));

    await user.selectOptions(screen.getByLabelText("Base currency"), "CNY");

    await waitFor(() => {
      expect(totalAssetsValue()).toHaveTextContent("21,000.00 CNY");
    });
    expect(getPortfolioSummary).toHaveBeenCalledWith("CNY");
  });

  it("shows a refresh error, reverts the switcher, and keeps the last good data when the currency refetch fails", async () => {
    // Grok review round 1 (PR #322): a failed refetch used to leave the
    // switcher showing the newly-picked currency while every figure on the
    // page stayed in the old one — this pins the revert.
    const user = userEvent.setup();
    getPortfolioSummary.mockRejectedValue(new Error("boom"));
    renderBody(summary({}));

    await user.selectOptions(screen.getByLabelText("Base currency"), "CNY");

    await waitFor(() => {
      expect(
        screen.getByText("Couldn't refresh for the new currency. Showing the last loaded values."),
      ).toBeInTheDocument();
    });
    // Last good total is still shown, not wiped out by the failed refetch.
    expect(totalAssetsValue()).toHaveTextContent("3,000.00 USD");
    // The switcher reverted to USD — it no longer disagrees with the data.
    expect(screen.getByLabelText("Base currency")).toHaveValue("USD");
  });

  it("shows a by-broker (custodian) card and a separate by-account card (issue #330)", () => {
    renderBody(
      summary({
        by_broker: { Fidelity: "3000.00" },
        by_account: { "Individual Brokerage": "3000.00" },
      }),
    );

    const custodianCard = screen.getByText("By custodian").closest('[data-slot="card"]');
    if (!custodianCard) throw new Error("Custodian card not found");
    expect(within(custodianCard as HTMLElement).getByText("Fidelity")).toBeInTheDocument();

    const accountCard = screen.getByText("By account").closest('[data-slot="card"]');
    if (!accountCard) throw new Error("Account card not found");
    expect(within(accountCard as HTMLElement).getByText("Individual Brokerage")).toBeInTheDocument();
  });

  it("no longer renders a by-sector card (issue #330)", () => {
    renderBody(summary({}));
    expect(screen.queryByText("By sector")).not.toBeInTheDocument();
  });

  it("defaults the currency card to normalized (by_currency, unchanged behavior)", () => {
    renderBody(summary({ by_currency: { USD: "3000.00" } }));

    const currencyCard = screen.getByText("By currency").closest('[data-slot="card"]');
    if (!currencyCard) throw new Error("Currency card not found");
    const rows = within(currencyCard as HTMLElement).getAllByRole("listitem");
    expect(rows.some((row) => row.textContent?.includes("3,000.00 USD"))).toBe(true);
    expect(screen.getByLabelText("Display")).toHaveValue("normalized");
  });

  it("switches the currency card to native mode, summing each holding's own currency", async () => {
    const user = userEvent.setup();
    const usdHolding = priced({ holding_id: "h-usd", currency: "USD", market_value: "3000.00" });
    const cnyHolding = priced({
      holding_id: "h-cny",
      currency: "CNY",
      market_value: "7000.00",
      market_value_base: "1000.00",
    });
    renderBody(
      summary({
        holdings: [usdHolding, cnyHolding],
        by_currency: { USD: "3000.00", CNY: "1000.00" },
      }),
    );

    await user.selectOptions(screen.getByLabelText("Display"), "native");

    const currencyCard = screen.getByText("By currency").closest('[data-slot="card"]');
    if (!currencyCard) throw new Error("Currency card not found");
    const rows = within(currencyCard as HTMLElement).getAllByRole("listitem");
    // Native mode sums each bucket's own market_value, unconverted — CNY
    // shows its native 7000.00, not the 1000.00 base-currency figure.
    expect(rows.some((row) => row.textContent?.includes("7,000.00 CNY"))).toBe(true);
    expect(rows.some((row) => row.textContent?.includes("3,000.00 USD"))).toBe(true);
    // Issue #330 review round 1 (blocker 1): native mode mixes incommensurable
    // currencies, so the card must not size a pie from those raw numbers.
    expect(
      (currencyCard as HTMLElement).querySelector(".recharts-responsive-container"),
    ).not.toBeInTheDocument();
  });

  it("excludes a holding with a stale FX rate from native mode, matching normalized mode's membership", async () => {
    // Issue #330 review round 1 (blocker 2): a holding with a native
    // market_value but a null market_value_base (e.g. a stale FX pair) is
    // already excluded from by_currency — native mode must apply the same
    // gate, or switching modes would add/remove a bucket.
    const user = userEvent.setup();
    const staleFxHolding = priced({
      holding_id: "h-stale",
      currency: "GBP",
      market_value: "590.00",
      market_value_base: null,
    });
    renderBody(
      summary({
        holdings: [priced({}), staleFxHolding],
        by_currency: { USD: "3000.00" },
      }),
    );

    await user.selectOptions(screen.getByLabelText("Display"), "native");

    const currencyCard = screen.getByText("By currency").closest('[data-slot="card"]');
    if (!currencyCard) throw new Error("Currency card not found");
    expect(within(currencyCard as HTMLElement).queryByText(/GBP/)).not.toBeInTheDocument();
  });

  it("switches the currency card to percentage mode, showing each bucket's share of the total", async () => {
    const user = userEvent.setup();
    renderBody(summary({ by_currency: { USD: "3000.00", CNY: "1000.00" } }));

    await user.selectOptions(screen.getByLabelText("Display"), "percentage");

    const currencyCard = screen.getByText("By currency").closest('[data-slot="card"]');
    if (!currencyCard) throw new Error("Currency card not found");
    const rows = within(currencyCard as HTMLElement).getAllByRole("listitem");
    expect(rows.some((row) => row.textContent?.includes("75.0%"))).toBe(true);
    expect(rows.some((row) => row.textContent?.includes("25.0%"))).toBe(true);
    // showShareOfTotal is suppressed in percentage mode, so the value isn't
    // duplicated as "75.0% (100.0%)".
    expect(rows.every((row) => !row.textContent?.includes("("))).toBe(true);
  });

  it("disables the switcher for the whole round trip, not just the synchronous dispatch", async () => {
    // Grok review round 1 (PR #322): the fetch used to run inside
    // `startTransition(() => { void promise.then(...) })` — isPending only
    // covered the synchronous scheduling call, so disabled={isPending} was
    // effectively a no-op and a second click could race the first response.
    // Awaiting inside an async startTransition scope keeps isPending (and
    // thus disabled) true for the entire in-flight fetch.
    const user = userEvent.setup();
    let resolveFetch!: (value: PortfolioSummary) => void;
    getPortfolioSummary.mockReturnValue(
      new Promise<PortfolioSummary>((resolve) => {
        resolveFetch = resolve;
      }),
    );
    renderBody(summary({}));

    await user.selectOptions(screen.getByLabelText("Base currency"), "CNY");
    expect(screen.getByLabelText("Base currency")).toBeDisabled();

    resolveFetch(summary({ base_currency: "CNY", total_base: "21000.00" }));
    await waitFor(() => {
      expect(screen.getByLabelText("Base currency")).not.toBeDisabled();
    });
  });
});
