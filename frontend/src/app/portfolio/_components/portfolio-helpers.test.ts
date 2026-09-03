import { describe, expect, it } from "vitest";

import type { HoldingValueOut } from "@/lib/api";
import {
  fallbackOrValue,
  formatMoney,
  formatPercent,
  isNoLivePrice,
  partitionHoldings,
  pnlColorClass,
} from "./portfolio-helpers";

function holding(overrides: Partial<HoldingValueOut>): HoldingValueOut {
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
    broker: null,
    account: null,
    portfolio: null,
    avg_cost: null,
    shares: "10",
    notes: null,
    cost_basis_base: null,
    unrealized_pnl_base: null,
    unrealized_pnl_pct: null,
    ...overrides,
  };
}

describe("isNoLivePrice", () => {
  it("is true only for auto-priced holdings the capture layer can't price", () => {
    expect(isNoLivePrice(holding({ pricing_mode: "auto", capture_supported: false }))).toBe(
      true,
    );
  });

  it("is false for a normally-priced auto holding", () => {
    expect(isNoLivePrice(holding({ pricing_mode: "auto", capture_supported: true }))).toBe(
      false,
    );
  });

  it("is false for cash/wmf (manual) holdings even without capture support", () => {
    // Issue #320 decision 3: the partition must not use the broader
    // market_value_base==null signal — manual holdings are never "no live
    // price", they just don't have an auto-fetched price by design.
    expect(isNoLivePrice(holding({ pricing_mode: "manual", capture_supported: false }))).toBe(
      false,
    );
  });
});

describe("partitionHoldings", () => {
  it("splits priced holdings from capture-unsupported ones, preserving order", () => {
    const priced = holding({ holding_id: "priced" });
    const cash = holding({ holding_id: "cash", pricing_mode: "manual" });
    const unsupported = holding({
      holding_id: "unsupported",
      pricing_mode: "auto",
      capture_supported: false,
      market_value_base: null,
    });

    const { priced: pricedOut, noLivePrice } = partitionHoldings([
      priced,
      unsupported,
      cash,
    ]);

    expect(pricedOut.map((h) => h.holding_id)).toEqual(["priced", "cash"]);
    expect(noLivePrice.map((h) => h.holding_id)).toEqual(["unsupported"]);
  });

  it("returns empty arrays for an empty input", () => {
    expect(partitionHoldings([])).toEqual({ priced: [], noLivePrice: [] });
  });
});

describe("formatMoney", () => {
  it("renders '—' for null", () => {
    expect(formatMoney(null, "USD")).toBe("—");
  });

  it("formats with thousands separators and the currency code, not a symbol", () => {
    // Avoids Intl.NumberFormat currency-style entirely: CNH is not a real
    // ISO 4217 code and symbol ambiguity (USD/CAD/AUD/SGD/HKD all use "$")
    // would be actively misleading on a multi-currency dashboard.
    expect(formatMoney("1234567.8", "USD")).toBe("1,234,567.80 USD");
    expect(formatMoney("100", "CNH")).toBe("100.00 CNH");
  });

  it("keeps the sign for a negative value", () => {
    expect(formatMoney("-500", "USD")).toBe("-500.00 USD");
  });
});

describe("formatPercent", () => {
  it("renders '—' for null", () => {
    expect(formatPercent(null)).toBe("—");
  });

  it("formats a ratio as a signed percentage", () => {
    expect(formatPercent("0.2")).toBe("+20.00%");
    expect(formatPercent("-0.0569")).toBe("-5.69%");
    expect(formatPercent("0")).toBe("+0.00%");
  });
});

describe("fallbackOrValue", () => {
  it("returns the translated fallback for null or empty, matching the backend's `h.field or literal`", () => {
    expect(fallbackOrValue(null, "Ungrouped label")).toBe("Ungrouped label");
    expect(fallbackOrValue("", "Ungrouped label")).toBe("Ungrouped label");
  });

  it("returns the real value when present", () => {
    expect(fallbackOrValue("Retirement", "Ungrouped label")).toBe("Retirement");
  });
});

describe("pnlColorClass", () => {
  it("uses a neutral tone for null (not-applicable, e.g. cash/wmf)", () => {
    expect(pnlColorClass(null)).toBe("text-foreground/60");
  });

  it("uses green for a gain and the destructive token for a loss", () => {
    expect(pnlColorClass("100")).toContain("emerald");
    expect(pnlColorClass("-1")).toBe("text-destructive");
    expect(pnlColorClass("0")).toBe("text-foreground");
  });
});
