import { SPY_AGG_CLOSES } from "../_data/spy-agg-closes";
import { STYLE_BASIS_CLOSES, STYLE_FX } from "../_data/style-basis-closes";
import { replayMetrics } from "./example";

export const STYLE_SYMBOLS = ["IWF", "IWD", "IWM", "EFA", "EEM", "2800.HK", "510300.SS", "AGG", "TLT", "GLD", "DBC", "VNQ", "BIL"] as const;
export const SCENARIO_IDS = ["dotcom_2000", "gfc_2008", "covid_2020", "rates_2022", "tariffs_2025"] as const;
const mean = (v: readonly number[]) => v.reduce((s, x) => s + x, 0) / v.length;
const variance = (v: readonly number[]) => { const m = mean(v); return v.reduce((s, x) => s + (x - m) ** 2, 0) / (v.length - 1); };
const time = (day: string) => Date.parse(`${day}T00:00:00Z`);
const dot = (a: readonly number[], b: readonly number[]) => a.reduce((s, v, i) => s + v * b[i], 0);
export const roundSix = (v: number) => Math.round(v * 1e6) / 1e6;
export const rSquaredDisplay = (v: number) => (Math.floor(Math.round(v * 1e6) / 1e4) / 100).toFixed(2);

export function priceOn(series: readonly (readonly [string, number])[], day: string): number | null {
  let lo = 0, hi = series.length;
  while (lo < hi) { const mid = Math.floor((lo + hi) / 2); if (series[mid][0] <= day) lo = mid + 1; else hi = mid; }
  const row = series[lo - 1];
  return row && time(day) - time(row[0]) <= 10 * 86400000 ? row[1] : null;
}

function solve(matrix: number[][], rhs: number[]) {
  const a = matrix.map((row, i) => [...row, rhs[i]]), n = rhs.length;
  for (let col = 0; col < n; col++) {
    let pivot = col;
    for (let row = col + 1; row < n; row++) if (Math.abs(a[row][col]) > Math.abs(a[pivot][col])) pivot = row;
    [a[col], a[pivot]] = [a[pivot], a[col]];
    if (a[col][col] === 0) throw new Error("Singular style system");
    for (let row = col + 1; row < n; row++) {
      const factor = a[row][col] / a[col][col];
      for (let j = col; j <= n; j++) a[row][j] -= factor * a[col][j];
    }
  }
  const out = Array<number>(n).fill(0);
  for (let row = n - 1; row >= 0; row--) out[row] = (a[row][n] - a[row].slice(row + 1, n).reduce((s, v, i) => s + v * out[row + 1 + i], 0)) / a[row][row];
  return out;
}

/** Faithful primal active-set port of jade_style.constrained_weights. */
export function constrainedWeights(x: number[][], y: number[]) {
  const k = x[0].length, tol = 1e-12;
  const q = Array.from({ length: k }, (_, i) => Array.from({ length: k }, (_, j) => x.reduce((s, row) => s + row[i] * row[j], 0)));
  const c = Array.from({ length: k }, (_, i) => x.reduce((s, row, n) => s + row[i] * y[n], 0));
  const distances = Array.from({ length: k }, (_, i) => x.reduce((s, row, n) => s + (y[n] - row[i]) ** 2, 0));
  const first = distances.indexOf(Math.min(...distances));
  const w = Array<number>(k).fill(0); w[first] = 1;
  const free = new Set([first]);
  for (let iteration = 0; iteration < 10 * k; iteration++) {
    const ix = [...free].sort((a, b) => a - b);
    const a = ix.map(i => [...ix.map(j => q[i][j]), 1]); a.push([...ix.map(() => 1), 0]);
    const solution = solve(a, [...ix.map(i => c[i]), 1]);
    const z = solution.slice(0, -1), nu = solution[solution.length - 1];
    if (z.every(v => v >= -tol)) {
      w.fill(0); ix.forEach((i, j) => { w[i] = z[j]; });
      const fixed = Array.from({ length: k }, (_, i) => i).filter(i => !free.has(i));
      const multipliers = q.map((row, i) => dot(row, w) - c[i] + nu);
      if (!fixed.length || Math.min(...fixed.map(i => multipliers[i])) >= -tol) return w.map(v => Math.max(v, 0));
      let release = fixed[0];
      for (const i of fixed) if (multipliers[i] < multipliers[release]) release = i;
      free.add(release);
    } else {
      const alpha = Math.min(1, ...ix.flatMap((i, j) => z[j] < 0 ? [w[i] / (w[i] - z[j])] : []));
      ix.forEach((i, j) => { w[i] += alpha * (z[j] - w[i]); });
      for (const i of ix) if (w[i] <= tol) { w[i] = 0; free.delete(i); }
    }
  }
  throw new Error("Style active-set solver exceeded its iteration limit");
}

export function stressExample() {
  const rows = SPY_AGG_CLOSES.filter(r => r[0] >= "2024-08-19" && r[0] <= "2025-10-08");
  const dates = rows.map(r => r[0]);
  const v = rows.map(r => .6 * r[1] / rows[0][1] + .4 * r[2] / rows[0][2]);
  const b = rows.map(r => r[1] / rows[0][1]);
  const peak = dates.indexOf("2025-02-19"), trough = dates.indexOf("2025-04-08");
  const shock = v[trough] / v[peak] - 1;
  return {
    start: dates[0], end: dates[dates.length - 1], peak: dates[peak], trough: dates[trough], returns: rows.length - 1,
    shock, spyShock: b[trough] / b[peak] - 1, amount: shock * 100000,
    portfolioMetrics: replayMetrics(dates, v), spyMetrics: replayMetrics(dates, b),
    peakSpyWeight: .6 * rows[peak][1] / rows[0][1] / v[peak],
    contributions: [
      { assetClass: "EQUITY_US_BROAD", value: .6 * (rows[trough][1] - rows[peak][1]) / rows[0][1] / v[peak] },
      { assetClass: "BOND_FUND", value: .4 * (rows[trough][2] - rows[peak][2]) / rows[0][2] / v[peak] },
    ].sort((a, b) => a.value - b.value),
    points: dates.map((date, i) => ({ date, time: time(date), portfolio: v[i] / v[peak] - 1, spy: b[i] / b[peak] - 1 })),
  };
}

function fit(x: number[][], y: number[]) {
  const weights = constrainedWeights(x, y);
  const residuals = y.map((v, i) => v - dot(x[i], weights));
  const rSquared = 1 - variance(residuals) / variance(y);
  return { weights, displayWeights: weights.map(roundSix), rSquared, rSquaredText: rSquaredDisplay(rSquared), residualVol: Math.sqrt(variance(residuals)) * Math.sqrt(252 / 3) };
}
export function styleExample() {
  const rows = SPY_AGG_CLOSES.filter(r => r[0] >= "2026-07-09").flatMap(row => {
    const levels: number[] = [];
    for (const symbol of STYLE_SYMBOLS) {
      const close = priceOn(STYLE_BASIS_CLOSES[symbol], row[0]);
      const rate = symbol === "2800.HK" ? priceOn(STYLE_FX.USDHKD, row[0]) : symbol === "510300.SS" ? priceOn(STYLE_FX.USDCNY, row[0]) : 1;
      if (close === null || rate === null) return [];
      levels.push(close / rate);
    }
    return [{ row, levels }];
  });
  const levels = rows.map(r => r.levels), dates = rows.map(r => r.row[0]);
  const v = rows.map(r => .6 * r.row[1] / rows[0].row[1] + .4 * r.row[2] / rows[0].row[2]);
  const b = rows.map(r => r.row[1] / rows[0].row[1]);
  const horizon = 3;
  const x = levels.slice(horizon).map((row, i) => row.map((v, j) => v / levels[i][j] - 1));
  const y = v.slice(horizon).map((value, i) => value / v[i] - 1);
  const by = b.slice(horizon).map((v, i) => v / b[i] - 1);
  const portfolio = fit(x, y), spy = fit(x, by);
  return { start: dates[0], end: dates[dates.length - 1], sampleCount: y.length, x, y, portfolio, spy,
    listedSymbols: STYLE_SYMBOLS.filter((_, i) => roundSix(portfolio.weights[i]) > 0 || roundSix(spy.weights[i]) > 0),
    points: dates.map((date, i) => ({ date, time: time(date), portfolio: v[i] / v[0] - 1, styleMix: dot(portfolio.weights, levels[i].map((v, j) => v / levels[0][j])) - 1 })),
  };
}

function trackingSpan(start: string) {
  const rows = SPY_AGG_CLOSES.filter(r => r[0] >= start), dates = rows.map(r => r[0]);
  const p = rows.map(r => .6 * r[1] / rows[0][1] + .4 * r[2] / rows[0][2]);
  const balancedIndex = [1], spyIndex = rows.map(r => r[1] / rows[0][1]);
  rows.slice(1).forEach((r, i) => { balancedIndex.push(balancedIndex[i] * (1 + .6 * (r[1] / rows[i][1] - 1) + .4 * (r[2] / rows[i][2] - 1))); });
  const calendarDays = (time(dates[dates.length - 1]) - time(dates[0])) / 86400000;
  const horizon = 5;
  function metrics(index: number[]) {
    const portfolioReturn = p[p.length - 1] / p[0] - 1, referenceReturn = index[index.length - 1] - 1;
    const active = p.slice(horizon).map((v, i) => v / p[i] - index[i + horizon] / index[i]);
    return { portfolioReturn, referenceReturn, excessReturn: portfolioReturn - referenceReturn,
      annualizedExcess: (1 + portfolioReturn) ** (365.25 / calendarDays) - (1 + referenceReturn) ** (365.25 / calendarDays),
      trackingError: Math.sqrt(variance(active)) * Math.sqrt(252 / horizon) };
  }
  return { dates, calendarDays, sampleCount: dates.length - horizon, balanced: metrics(balancedIndex), spy: metrics(spyIndex),
    points: dates.map((date, i) => ({ date, time: time(date), balanced: p[i] / p[0] - balancedIndex[i], spy: p[i] / p[0] - spyIndex[i] })),
  };
}
export function trackingExample() {
  const oneYear = trackingSpan("2025-10-09"), fiveYear = trackingSpan("2021-10-09");
  const last = SPY_AGG_CLOSES[SPY_AGG_CLOSES.length - 1], first = SPY_AGG_CLOSES[0];
  const endSpyWeight = (.6 * last[1] / first[1]) / (.6 * last[1] / first[1] + .4 * last[2] / first[2]);
  return { oneYear, fiveYear, points: fiveYear.points, endSpyWeight };
}
export type StressExample = ReturnType<typeof stressExample>;
export type StyleExample = ReturnType<typeof styleExample>;
export type TrackingExample = ReturnType<typeof trackingExample>;
