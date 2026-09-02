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
    by_account: { Fidelity: "3000.00" },
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
    });
    renderBody(summary({ holdings: [priced({}), unsupported] }));

    expect(screen.getByText("No live price available")).toBeInTheDocument();
    expect(screen.getByText("Unresolvable")).toBeInTheDocument();
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

  it("shows a refresh error but keeps the last good data when the currency refetch fails", async () => {
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
  });
});
