import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { HoldingOut } from "@/lib/api";
import { HoldingsTable } from "./holdings-table";

function holding(overrides: Partial<HoldingOut>): HoldingOut {
  return {
    id: "h1",
    name: "Apple",
    ticker: "AAPL",
    fund_code: null,
    currency: "USD",
    shares: "10",
    avg_cost: "150",
    current_value: "3000",
    pricing_mode: "auto",
    asset_type: "stock",
    asset_class: "US_EQUITY",
    market: "US",
    capture_supported: true,
    broker: null,
    account: null,
    portfolio: null,
    notes: null,
    watch_tier: null,
    last_manual_update: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    position: 0,
    ...overrides,
  };
}

describe("HoldingsTable", () => {
  it("shows the watch-tier icon next to the ticker when set (issue #430)", () => {
    render(
      <LocaleProvider>
        <HoldingsTable holdings={[holding({ watch_tier: "focus" })]} />
      </LocaleProvider>,
    );

    expect(screen.getByRole("img", { name: "Focus" })).toBeInTheDocument();
  });

  it("shows no watch-tier icon when unset", () => {
    render(
      <LocaleProvider>
        <HoldingsTable holdings={[holding({})]} />
      </LocaleProvider>,
    );

    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
