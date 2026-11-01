// Chart row building for the asset-class allocation history card (issue
// #433). Kept separate from the recharts component so the stacking/gap
// logic is unit-testable without rendering.

import type { Allocation } from "@/lib/api";
import { toRatio } from "./performance-format";

export interface AllocationRow {
  date: string;
  // Ratio (0-1) per asset_class key, or null on a gap date (zero
  // denominator) — never a fabricated 0 that would render as a real,
  // valued zero-weight class.
  [assetClass: string]: string | number | null;
}

export interface AllocationPointMeta {
  isIncomplete: boolean;
  excludedHoldingCount: number;
  isGap: boolean;
}

export interface BuiltAllocationData {
  rows: AllocationRow[];
  // Closed-taxonomy keys present anywhere in this response, in the
  // backend's fixed display order (never re-sorted by value/rank).
  assetClasses: string[];
  hasIncomplete: boolean;
  pointMeta: Record<string, AllocationPointMeta>;
}

export function buildAllocationData(allocation: Allocation | null): BuiltAllocationData {
  if (!allocation || allocation.points.length === 0) {
    return { rows: [], assetClasses: [], hasIncomplete: false, pointMeta: {} };
  }
  const assetClasses = allocation.asset_classes;
  let hasIncomplete = false;
  const pointMeta: Record<string, AllocationPointMeta> = {};

  const rows: AllocationRow[] = allocation.points.map((point) => {
    const isGap = Object.keys(point.weights).length === 0;
    if (point.is_incomplete) hasIncomplete = true;
    pointMeta[point.date] = {
      isIncomplete: point.is_incomplete,
      excludedHoldingCount: point.excluded_holding_count,
      isGap,
    };
    const row: AllocationRow = { date: point.date };
    for (const cls of assetClasses) {
      row[cls] = isGap ? null : (toRatio(point.weights[cls]) ?? 0);
    }
    return row;
  });

  return { rows, assetClasses, hasIncomplete, pointMeta };
}

// A fixed categorical palette keyed by taxonomy identity (not by array
// index within one response's filtered `asset_classes`) — a class keeps its
// color whether the current range/filters show 2 classes or all 13 (issue
// #433 requirement 2: "do not dynamically recolor classes by rank").
// Chosen for readability on both the light and dark card backgrounds this
// app supports (see globals.css).
export const ASSET_CLASS_COLORS: Record<string, string> = {
  STOCK: "var(--asset-stock)",
  EQUITY_US_BROAD: "var(--asset-equity-us-broad)",
  EQUITY_US_TECH: "var(--asset-equity-us-tech)",
  EQUITY_DM: "var(--asset-equity-dm)",
  EQUITY_CN: "var(--asset-equity-cn)",
  EQUITY_EM: "var(--asset-equity-em)",
  EQUITY_BROAD: "var(--asset-equity-broad)",
  REIT: "var(--asset-reit)",
  PRECIOUS_METALS: "var(--asset-precious-metals)",
  ENERGY: "var(--asset-energy)",
  COMMODITY: "var(--asset-commodity)",
  BOND_FUND: "var(--asset-bond-fund)",
  CASH_EQUIV: "var(--asset-cash-equiv)",
};

export const DEFAULT_ASSET_CLASS_COLOR = "var(--asset-default)";
