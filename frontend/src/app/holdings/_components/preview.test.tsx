import { describe, expect, it } from "vitest";

import type { ParsedRow } from "@/lib/api";
import { rowNeedsAmber } from "./preview";

function row(partial: Partial<ParsedRow> = {}): ParsedRow {
  return {
    name: "Cash",
    ticker: null,
    fund_code: null,
    currency: "USD",
    shares: null,
    avg_cost: null,
    current_value: 1000,
    pricing_mode: "manual",
    asset_type: "cash",
    broker: "CMB",
    account: null,
    portfolio: null,
    notes: null,
    issues: [],
    confidence: 1,
    ...partial,
  };
}

describe("rowNeedsAmber", () => {
  it("does not highlight high-confidence cash with only info notes", () => {
    expect(
      rowNeedsAmber(
        row({
          issues: [
            { code: "cash_amount_moved", params: {}, severity: "info" },
            { code: "dropped_spurious_id", params: { identifier: "CASH" }, severity: "info" },
          ],
        }),
      ),
    ).toBe(false);
  });

  it("highlights a warning even at high confidence", () => {
    expect(
      rowNeedsAmber(
        row({
          name: "PSH",
          ticker: "PSH",
          pricing_mode: "auto",
          asset_type: "stock",
          issues: [
            {
              code: "ticker_no_suffix",
              params: { ticker: "PSH", currency: "GBP", suggestion: "PSH.L" },
              severity: "warning",
            },
          ],
        }),
      ),
    ).toBe(true);
  });

  it("highlights low confidence even without warnings", () => {
    expect(rowNeedsAmber(row({ confidence: 0.69, issues: [] }))).toBe(true);
  });
});
