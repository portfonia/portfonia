import { expect, it } from "vitest";

it("737 A1 every independently computed value, dates, raw rows and histogram", async () => {
  const { SPY_AGG_CLOSES: rows } = await import("../_data/spy-agg-closes");
  const { calculateExample } = await import("./example");
  expect(rows).toHaveLength(1255);
  expect(rows[0]).toEqual(["2021-10-11", 406.566650390625, 96.59980010986328]);
  expect(rows.at(-1)).toEqual(["2026-10-09", 778.5700073242188, 94.58999633789062]);
  const e = calculateExample();
  const expected = [
    { metrics: [.540670,.090406,.117626,-.210280,-.038794,-.072334], peak: "2021-12-27", trough: "2022-10-14", tails: [[.011437,.016631,.049399,.065870,.011816,.014912,.044888,.058132],[.020141,.026219,.074498,.082198,.016865,.019376,.066488,.077228]], fat: 9 },
    { metrics: [.914987,.138945,.171471,-.244964,-.058543,-.092446], peak: "2022-01-03", trough: "2022-10-12", tails: [[.016528,.024610,.072320,.090515,.017191,.021704,.060905,.079199],[.029479,.038781,.106869,.115591,.024552,.028212,.090740,.105576]], fat: 11 },
  ];
  [e.portfolio,e.spy].forEach((series,index) => {
    const x=expected[index], m=series.metrics;
    [m.cumulative,m.annualized,m.volatility,m.drawdown,m.worstDay,m.worstMonth].forEach((v,i)=>expect(v).toBeCloseTo(x.metrics[i],6));
    expect([m.peakDate,m.troughDate,m.worstDayDate,m.worstMonthLabel]).toEqual([x.peak,x.trough,"2025-04-04","2022-09"]);
    expect([series.tail.dailyCount,series.tail.monthCount,series.tail.independentMonths]).toEqual([1254,1234,59]);
    series.tail.levels.forEach((l,i)=> {
      [l.daily.var,l.daily.cvar,l.monthly.var,l.monthly.cvar,l.normalDaily.var,l.normalDaily.cvar,l.normalMonthly.var,l.normalMonthly.cvar].forEach((v,j)=>expect(v).toBeCloseTo(x.tails[i][j],6));
      expect([l.daily.k,l.monthly.k]).toEqual(i===0?[63,62]:[13,13]);
    });
    expect(series.tail.fatCount).toBe(x.fat);
    expect(series.tail.normalExpected).toBeCloseTo(1.692772,6);
  });
  expect(e.endSpyWeight).toBeCloseTo(.745774,6);
  expect(e.oneMonth.cumulative).toBeCloseTo(.012759,6);
  expect(e.oneMonth.annualized).toBeCloseTo(.166913,6);
  e.reference.forEach((r,i)=>{expect(r.daily).toBeCloseTo([.020723,.029309][i],6);expect(r.monthly).toBeCloseTo([.094966,.134312][i],6);});
  expect(e.histogram.map(b=>b.index)).toEqual(Array.from({length:22},(_,i)=>i-8));
  expect(e.histogram.map(b=>b.count)).toEqual([1,2,4,6,15,59,146,362,374,192,68,19,4,0,0,0,1,0,0,0,0,1]);
});
