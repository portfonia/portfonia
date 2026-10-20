import { SPY_AGG_CLOSES } from "../_data/spy-agg-closes";
import { replayMetrics } from "./example";
export function whatIfExample() {
  const dates = SPY_AGG_CLOSES.map(row => row[0]);
  const constants = { spyStart: .6, aggStart: .4 };
  const none = SPY_AGG_CLOSES.map(row => constants.spyStart * row[1] / SPY_AGG_CLOSES[0][1] + constants.aggStart * row[2] / SPY_AGG_CLOSES[0][2]);
  function rebalanced(rule: "monthly" | "quarterly") {
    let spyUnits = constants.spyStart / SPY_AGG_CLOSES[0][1], aggUnits = constants.aggStart / SPY_AGG_CLOSES[0][2];
    let resets = 0;
    const period = (day: string) => rule === "monthly" ? day.slice(0, 7) : `${day.slice(0, 4)}-${Math.floor((Number(day.slice(5, 7)) - 1) / 3)}`;
    const values = SPY_AGG_CLOSES.map((row, i) => {
      const value = spyUnits * row[1] + aggUnits * row[2];
      if (i > 0 && period(row[0]) !== period(SPY_AGG_CLOSES[i - 1][0])) {
        spyUnits = constants.spyStart * value / row[1];
        aggUnits = constants.aggStart * value / row[2];
        resets += 1;
      }
      return value;
    });
    return { values, resets };
  }
  const monthly = rebalanced("monthly"), quarterly = rebalanced("quarterly");
  const last = SPY_AGG_CLOSES[SPY_AGG_CLOSES.length - 1];
  return {
    start: dates[0], end: dates[dates.length - 1], constants,
    none: replayMetrics(dates, none), monthly: replayMetrics(dates, monthly.values), quarterly: replayMetrics(dates, quarterly.values),
    monthlyResets: monthly.resets, quarterlyResets: quarterly.resets,
    endSpyWeight: (constants.spyStart * last[1] / SPY_AGG_CLOSES[0][1]) / none[none.length - 1],
    endAggWeight: (constants.aggStart * last[2] / SPY_AGG_CLOSES[0][2]) / none[none.length - 1],
    points: dates.map((date, i) => ({ date, time: Date.parse(date), none: none[i] / none[0] - 1, monthly: monthly.values[i] / monthly.values[0] - 1, quarterly: quarterly.values[i] / quarterly.values[0] - 1 })),
  };
}

export type WhatIfExample = ReturnType<typeof whatIfExample>;
