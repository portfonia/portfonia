import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { HoldingValueOut } from "@/lib/api";
import { PortfolioHoldingsTable } from "./portfolio-holdings-table";

function holding(overrides: Partial<HoldingValueOut>): HoldingValueOut {
  return {
    holding_id: "h1",
    name: "USD Cash",
    ticker: null,
    fund_code: null,
    currency: "USD",
    asset_type: "cash",
    asset_class: "CASH_EQUIV",
    sector: null,
    market: "Other",
    market_value: "1000.00",
    market_value_base: "1000.00",
    price_as_of: null,
    pricing_mode: "manual",
    capture_supported: true,
    broker: null,
    account: null,
    portfolio: null,
    avg_cost: null,
    shares: null,
    cost_basis_base: null,
    unrealized_pnl_base: null,
    unrealized_pnl_pct: null,
    ...overrides,
  };
}

describe("PortfolioHoldingsTable", () => {
  it("shows the same Ungrouped/Other fallback labels the by_group/by_account chart legends use, not a bare dash", () => {
    // Grok review round 1 (PR #322): a "—" here couldn't be matched back to
    // the "Ungrouped"/"Other" pie slice it rolls up into.
    render(
      <LocaleProvider>
        <PortfolioHoldingsTable holdings={[holding({})]} baseCurrency="USD" />
      </LocaleProvider>,
    );

    expect(screen.getByText("Ungrouped")).toBeInTheDocument();
    // "Other" legitimately appears twice here: the Market column (the
    // fixture's market really is "Other") and the Custodian fallback.
    expect(screen.getAllByText("Other").length).toBe(2);
  });

  it("renders the real group/broker name when set, not the fallback", () => {
    render(
      <LocaleProvider>
        <PortfolioHoldingsTable
          holdings={[
            holding({ portfolio: "Retirement", broker: "Fidelity", market: "US" }),
          ]}
          baseCurrency="USD"
        />
      </LocaleProvider>,
    );

    expect(screen.getByText("Retirement")).toBeInTheDocument();
    expect(screen.getByText("Fidelity")).toBeInTheDocument();
    expect(screen.queryByText("Ungrouped")).not.toBeInTheDocument();
    expect(screen.queryByText("Other")).not.toBeInTheDocument();
  });

  it("renders '—' for P&L when unavailable (cash/wmf)", () => {
    render(
      <LocaleProvider>
        <PortfolioHoldingsTable holdings={[holding({})]} baseCurrency="USD" />
      </LocaleProvider>,
    );

    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(2); // P&L amount + P&L %
  });
});
