import { expect, it } from "vitest";

it("739 A1 stress every Exploration value and contribution order", async () => {
  const { stressExample } = await import("./example-batch2");
  const e = stressExample();
  expect(e.points).toHaveLength(286);
  expect(e.returns).toBe(285);
  [e.shock, e.spyShock, e.amount, e.portfolioMetrics.drawdown, e.spyMetrics.drawdown,
    e.points.at(-1)?.portfolio, e.points.at(-1)?.spy, ...e.contributions.map(c => c.value), e.peakSpyWeight]
    .forEach((v, i) => expect(v).toBeCloseTo([-.113954, -.187552, -11395.412516, -.113954, -.187552, .087903, .107807, -.117273, .003319, .625282][i], 6));
  expect(e.contributions.map(c => c.assetClass)).toEqual(["EQUITY_US_BROAD", "BOND_FUND"]);
  expect(e.contributions.reduce((s, c) => s + c.value, 0)).toBeCloseTo(e.shock, 12);
  for (const m of [e.portfolioMetrics, e.spyMetrics]) expect([m.peakDate, m.troughDate]).toEqual(["2025-02-19", "2025-04-08"]);
});

it("739 A1 style every Exploration weight fit curve and listed row", async () => {
  const { styleExample, rSquaredDisplay } = await import("./example-batch2");
  const e = styleExample();
  expect(e.points).toHaveLength(66);
  expect(e.sampleCount).toBe(63);
  const expected = [
    [.291376, .297087, 0, 0, 0, .001016, 0, .353078, .010385, 0, 0, .002288, .044771],
    [.482109, .486780, 0, 0, 0, .000629, 0, 0, 0, 0, 0, 0, .030482],
  ];
  [e.portfolio, e.spy].forEach((f, i) => {
    f.weights.forEach((w, j) => expect(w).toBeCloseTo(expected[i][j], 6));
    expect(f.weights.reduce((s, w) => s + w, 0)).toBeCloseTo(1, 9);
    expect(f.rSquared).toBeCloseTo([.998453, .997883][i], 6);
    expect(f.residualVol).toBeCloseTo([.002930, .005101][i], 6);
    expect(rSquaredDisplay(f.rSquared)).toBe("0.99");
  });
  expect(e.listedSymbols).toEqual(["IWF", "IWD", "2800.HK", "AGG", "TLT", "VNQ", "BIL"]);
  for (const j of [2, 3, 4, 6, 9, 10]) expect(e.portfolio.weights[j]).toBeLessThan(5e-7);
  expect(e.points.at(-1)?.portfolio).toBeCloseTo(.012417, 6);
  expect(e.points.at(-1)?.styleMix).toBeCloseTo(.011212, 6);
});

it("739 A1 tracking every Exploration metric and extreme", async () => {
  const { trackingExample } = await import("./example-batch2");
  const e = trackingExample();
  const expected = [
    [.097300, .095217, .002083, .002085, .001988],
    [.097300, .172552, -.075252, -.075310, .044684],
    [.540670, .489448, .051223, .007358, .009687],
    [.540670, .914987, -.374317, -.048539, .054379],
  ];
  [e.oneYear, e.fiveYear].forEach((span, i) => {
    expect(span.dates).toHaveLength([252, 1255][i]);
    expect(span.sampleCount).toBe([247, 1250][i]);
    expect(span.calendarDays).toBe([365, 1824][i]);
    [span.balanced, span.spy].forEach((m, j) => [m.portfolioReturn, m.referenceReturn, m.excessReturn, m.annualizedExcess, m.trackingError]
      .forEach((v, k) => expect(v).toBeCloseTo(expected[i * 2 + j][k], 6)));
  });
  for (const [key, min, max, minDate, maxDate] of [["balanced", -.007471, .051909, "2023-05-04", "2026-10-06"], ["spy", -.375781, .016873, "2026-10-06", "2022-06-16"]] as const) {
    const points = e.points;
    const low = points.reduce((a, b) => b[key] < a[key] ? b : a);
    const high = points.reduce((a, b) => b[key] > a[key] ? b : a);
    expect(low[key]).toBeCloseTo(min, 6); expect(high[key]).toBeCloseTo(max, 6);
    expect(low.date).toBe(minDate); expect(high.date).toBe(maxDate);
  }
});

it("739 A2 negative unconstrained weight has constrained optimum; style satisfies KKT", async () => {
  const { constrainedWeights, styleExample } = await import("./example-batch2");
  expect(constrainedWeights([[1, 0], [0, 1]], [2, -1])).toEqual([1, 0]);
  const e = styleExample();
  const w = e.portfolio.weights;
  const gradient = w.map((_, j) => e.x.reduce((s, row, i) => s + row[j] * (row.reduce((v, x, k) => v + x * w[k], 0) - e.y[i]), 0));
  const active = w.findIndex(v => v > 1e-12);
  w.forEach((v, j) => { if (v === 0) expect(gradient[j] - gradient[active]).toBeGreaterThanOrEqual(-1e-9); });
});

it("739 I3 verified export row counts and carry boundaries", async () => {
  const { STYLE_BASIS_CLOSES, STYLE_FX } = await import("../_data/style-basis-closes");
  expect(Object.keys(STYLE_BASIS_CLOSES)).toEqual(["IWF", "IWD", "IWM", "EFA", "EEM", "2800.HK", "510300.SS", "AGG", "TLT", "GLD", "DBC", "VNQ", "BIL"]);
  for (const [symbol, rows] of Object.entries(STYLE_BASIS_CLOSES)) expect(rows).toHaveLength(symbol === "510300.SS" ? 88 : 92);
  for (const rows of Object.values(STYLE_FX)) expect(rows).toHaveLength(118);
  const { priceOn } = await import("./example-batch2");
  const rows = [["2026-07-01", 2], ["2026-07-20", 3]] as const;
  expect(priceOn(rows, "2026-06-30")).toBeNull(); expect(priceOn(rows, "2026-07-11")).toBe(2);
  expect(priceOn(rows, "2026-07-12")).toBeNull(); expect(priceOn(rows, "2026-07-20")).toBe(3);
});
