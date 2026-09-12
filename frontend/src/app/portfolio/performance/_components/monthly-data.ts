// Row building + adaptive tick selection for the monthly performance bar
// chart (issue #433). Kept separate from the recharts component so the
// logic is unit-testable without rendering.

import type { MonthlyPartialReason, MonthlyPerformance } from "@/lib/api";
import { toRatio } from "./performance-format";

export interface MonthlyBarRow {
  month: string; // "YYYY-MM"
  startDate: string;
  endDate: string;
  portfolio: number | null;
  benchmark: number | null;
  partialReason: MonthlyPartialReason | null;
  isApproximate: boolean;
  benchmarkUnavailableReason: string | null;
}

export function buildMonthlyRows(monthly: MonthlyPerformance | null): MonthlyBarRow[] {
  if (!monthly) return [];
  return monthly.points.map((point) => ({
    month: point.month,
    startDate: point.start_date,
    endDate: point.end_date,
    portfolio: toRatio(point.portfolio_return_pct),
    benchmark: toRatio(point.benchmark_return_pct),
    partialReason: point.partial_reason,
    isApproximate: point.is_approximate,
    benchmarkUnavailableReason: point.benchmark_unavailable_reason,
  }));
}

// Recharts' BarChart draws a default Tooltip `cursor` — an unstyled,
// full-plot-height Rectangle behind the hovered month — whenever `<Tooltip>`
// doesn't set an explicit `cursor`. That default has no theme-aware fill,
// so on this app's dark card it renders as a stark, near-white wash over
// the hovered bars (reported after #433 shipped; see issue #437). A
// subtle, theme-consistent highlight replaces it instead of disabling the
// cursor outright, so hovering still shows which month is active.
export const MONTHLY_CHART_CURSOR = { fill: "var(--muted-foreground)", fillOpacity: 0.12 } as const;

// Issue #433 requirement 8: 5Y/ALL ranges must not overlap month labels. Show
// every month up to a comfortable count, otherwise thin evenly so the first
// and last month always remain labeled.
const MAX_LABELED_TICKS = 12;

export function adaptiveMonthTicks(months: readonly string[]): string[] {
  if (months.length <= MAX_LABELED_TICKS) return [...months];
  const step = Math.ceil(months.length / MAX_LABELED_TICKS);
  const ticks = months.filter((_, i) => i % step === 0);
  const last = months[months.length - 1];
  if (ticks[ticks.length - 1] !== last) ticks.push(last);
  return ticks;
}
