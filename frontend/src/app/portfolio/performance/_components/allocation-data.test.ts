import { describe, expect, it } from "vitest";

import type { Allocation } from "@/lib/api";
import { ASSET_CLASS_COLORS, buildAllocationData } from "./allocation-data";

describe("buildAllocationData", () => {
  it("returns empty data for a null/empty allocation", () => {
    expect(buildAllocationData(null)).toEqual({
      rows: [],
      assetClasses: [],
      hasIncomplete: false,
      pointMeta: {},
    });
  });

  it("builds one row per date with numeric ratios per asset class", () => {
    const allocation: Allocation = {
      asset_classes: ["STOCK", "CASH_EQUIV"],
      points: [
        {
          date: "2026-09-01",
          weights: { STOCK: "0.6000", CASH_EQUIV: "0.4000" },
          is_incomplete: false,
          excluded_holding_count: 0,
        },
      ],
    };
    const built = buildAllocationData(allocation);
    expect(built.rows).toEqual([{ date: "2026-09-01", STOCK: 0.6, CASH_EQUIV: 0.4 }]);
    expect(built.assetClasses).toEqual(["STOCK", "CASH_EQUIV"]);
    expect(built.hasIncomplete).toBe(false);
    expect(built.pointMeta["2026-09-01"]).toEqual({
      isIncomplete: false,
      excludedHoldingCount: 0,
      isGap: false,
    });
  });

  it("marks incomplete points without altering the valued weights", () => {
    const allocation: Allocation = {
      asset_classes: ["STOCK"],
      points: [
        {
          date: "2026-09-01",
          weights: { STOCK: "1.0000" },
          is_incomplete: true,
          excluded_holding_count: 2,
        },
      ],
    };
    const built = buildAllocationData(allocation);
    expect(built.hasIncomplete).toBe(true);
    expect(built.pointMeta["2026-09-01"].excludedHoldingCount).toBe(2);
    expect(built.rows[0].STOCK).toBe(1);
  });

  it("renders a zero-denominator point as an all-null gap row, never a fabricated stack", () => {
    const allocation: Allocation = {
      asset_classes: ["STOCK", "CASH_EQUIV"],
      points: [
        { date: "2026-09-01", weights: { STOCK: "1.0000" }, is_incomplete: false, excluded_holding_count: 0 },
        { date: "2026-09-02", weights: {}, is_incomplete: true, excluded_holding_count: 1 },
      ],
    };
    const built = buildAllocationData(allocation);
    const gapRow = built.rows.find((row) => row.date === "2026-09-02");
    expect(gapRow).toEqual({ date: "2026-09-02", STOCK: null, CASH_EQUIV: null });
    expect(built.pointMeta["2026-09-02"].isGap).toBe(true);
  });

  it("keeps a stable, distinct color per taxonomy key across the whole closed taxonomy", () => {
    const keys = Object.keys(ASSET_CLASS_COLORS);
    expect(new Set(Object.values(ASSET_CLASS_COLORS)).size).toBe(keys.length);
  });
});
