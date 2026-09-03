import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { PortfolioSummary } from "@/lib/api";
import { PnlSummaryCard } from "./pnl-summary-card";

function summary(overrides: Partial<PortfolioSummary>): PortfolioSummary {
  return {
    base_currency: "USD",
    fx_date: "2026-01-02",
    total_base: "1000.00",
    by_market: {},
    by_currency: {},
    by_asset_type: {},
    by_sector: {},
    by_asset_class: {},
    by_group: {},
    by_account: {},
    total_cost_basis_base: "0",
    total_unrealized_pnl_base: "0",
    total_unrealized_pnl_pct: null,
    price_as_of_date: null,
    stale_tickers: [],
    holdings: [],
    ...overrides,
  };
}

function renderCard(s: PortfolioSummary) {
  return render(
    <LocaleProvider>
      <PnlSummaryCard summary={s} />
    </LocaleProvider>,
  );
}

describe("PnlSummaryCard", () => {
  it("renders '—' for all three fields on a cash-only book, not a misleading 0.00", () => {
    // Grok review round 3 (PR #322): total_cost_basis_base/total_unrealized_pnl_base
    // are non-optional Decimals that send "0" (not null) when no holding
    // contributed a cost basis — a bare formatMoney call painted "0.00 USD"
    // next to the percent's correctly-null "—", reading as a real zero.
    renderCard(summary({}));

    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBe(3);
    expect(screen.queryByText(/0\.00 USD/)).not.toBeInTheDocument();
  });

  it("renders real figures when at least one holding has a cost basis", () => {
    renderCard(
      summary({
        total_cost_basis_base: "2500.00",
        total_unrealized_pnl_base: "500.00",
        total_unrealized_pnl_pct: "0.2000",
      }),
    );

    expect(screen.getByText("2,500.00 USD")).toBeInTheDocument();
    expect(screen.getByText("500.00 USD")).toBeInTheDocument();
    expect(screen.getByText("+20.00%")).toBeInTheDocument();
  });
});
