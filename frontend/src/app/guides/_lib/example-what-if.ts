import { SPY_AGG_CLOSES } from "../_data/spy-agg-closes";
import { replayMetrics } from "./example";
export function whatIfExample() {
 const dates = SPY_AGG_CLOSES.map(row => row[0]);
 const none = SPY_AGG_CLOSES.map(row => .6 * row[1] / SPY_AGG_CLOSES[0][1] + .4 * row[2] / SPY_AGG_CLOSES[0][2]);
 let spyUnits = .6 / SPY_AGG_CLOSES[0][1], aggUnits = .4 / SPY_AGG_CLOSES[0][2];
 const quarterly = SPY_AGG_CLOSES.map((row, i) => {
  const value = spyUnits * row[1] + aggUnits * row[2];
  const previous = SPY_AGG_CLOSES[Math.max(0,i-1)][0];
  const period = (day: string) => `${day.slice(0,4)}-${Math.floor((Number(day.slice(5,7))-1)/3)}`;
  if (i > 0 && period(row[0]) !== period(previous)) { spyUnits = .6 * value / row[1]; aggUnits = .4 * value / row[2]; }
  return value;
 });
 return { start: dates[0], end: dates[dates.length-1], none: replayMetrics(dates, none), quarterly: replayMetrics(dates, quarterly), points: dates.map((date,i)=>({date,time:Date.parse(date),none:none[i]/none[0]-1,quarterly:quarterly[i]/quarterly[0]-1})) };
}

export type WhatIfExample = ReturnType<typeof whatIfExample>;
