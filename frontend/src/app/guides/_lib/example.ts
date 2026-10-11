import { SPY_AGG_CLOSES } from "../_data/spy-agg-closes";

function mean(values: readonly number[]) {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function standardDeviation(values: readonly number[]) {
  const average = mean(values);
  const squaredDeviations = values.reduce((sum, value) => sum + (value - average) ** 2, 0);
  return Math.sqrt(squaredDeviations / (values.length - 1));
}

function dailyReturns(values: readonly number[]) {
  return values.slice(1).map((value, index) => value / values[index] - 1);
}

function normalQuantile(level: 95 | 99) {
  return level === 95 ? 1.6448536269514722 : 2.3263478740408408;
}

function historicalTail(returns: number[], level: 95 | 99) {
  const sorted = [...returns].sort((a, b) => a - b);
  const k = Math.floor((returns.length * (100 - level) + 99) / 100);
  return { var: -sorted[k - 1], cvar: -mean(sorted.slice(0, k)), k };
}

function normalTail(returns: number[], level: 95 | 99) {
  const z = normalQuantile(level);
  const sigma = standardDeviation(returns);
  const average = mean(returns);
  return {
    var: z * sigma - average,
    cvar: sigma * Math.exp(-z * z / 2) / Math.sqrt(2 * Math.PI) / (1 - level / 100) - average,
  };
}

export function replayMetrics(dates: readonly string[], values: readonly number[]) {
  const daily = dailyReturns(values);
  let peak = values[0];
  let peakDate = dates[0];
  let drawdown = 0;
  let drawdownPeak = dates[0];
  let drawdownTrough = dates[0];
  const months = new Map<string, number>();

  values.forEach((value, index) => {
    if (value > peak) {
      peak = value;
      peakDate = dates[index];
    }
    const decline = value / peak - 1;
    if (decline < drawdown) {
      drawdown = decline;
      drawdownPeak = peakDate;
      drawdownTrough = dates[index];
    }
    months.set(dates[index].slice(0, 7), value);
  });

  let previous = values[0];
  let worstMonth = Infinity;
  let worstMonthLabel = "";
  months.forEach((value, month) => {
    const monthlyReturn = value / previous - 1;
    if (monthlyReturn < worstMonth) {
      worstMonth = monthlyReturn;
      worstMonthLabel = month;
    }
    previous = value;
  });

  const worstDay = Math.min(...daily);
  const ratio = values[values.length - 1] / values[0];
  const calendarDays = (Date.parse(dates[dates.length - 1]) - Date.parse(dates[0])) / 86400000;
  return {
    cumulative: ratio - 1,
    annualized: ratio ** (365.25 / calendarDays) - 1,
    volatility: standardDeviation(daily) * Math.sqrt(252),
    drawdown,
    peakDate: drawdownPeak,
    troughDate: drawdownTrough,
    worstDay,
    worstDayDate: dates[daily.indexOf(worstDay) + 1],
    worstMonth,
    worstMonthLabel,
  };
}

export function tailRisk(values: readonly number[]) {
  const daily = dailyReturns(values);
  const monthly = values.slice(21).map((value, index) => value / values[index] - 1);
  const threeDeviationBoundary = mean(daily) - 3 * standardDeviation(daily);
  return {
    dailyCount: daily.length,
    monthCount: monthly.length,
    independentMonths: Math.floor(daily.length / 21),
    fatCount: daily.filter(value => value < threeDeviationBoundary).length,
    normalExpected: daily.length * 0.0013498980316301,
    levels: ([95, 99] as const).map(level => ({
      level,
      daily: historicalTail(daily, level),
      monthly: historicalTail(monthly, level),
      normalDaily: normalTail(daily, level),
      normalMonthly: normalTail(monthly, level),
    })),
  };
}

function histogram(returns: readonly number[]) {
  const counts = new Map<number, number>();
  returns.forEach(value => {
    const index = Math.floor(value / 0.005);
    counts.set(index, (counts.get(index) ?? 0) + 1);
  });
  const minimum = Math.min(...counts.keys());
  const maximum = Math.max(...counts.keys());
  return Array.from({ length: maximum - minimum + 1 }, (_, offset) => {
    const index = minimum + offset;
    return { index, lower: index * 0.005, count: counts.get(index) ?? 0 };
  });
}

export function calculateExample() {
  const dates = SPY_AGG_CLOSES.map(row => row[0]);
  const spy = SPY_AGG_CLOSES.map(row => row[1] / SPY_AGG_CLOSES[0][1]);
  const values = SPY_AGG_CLOSES.map((row, index) => 0.6 * spy[index] + 0.4 * row[2] / SPY_AGG_CLOSES[0][2]);
  const datedValues = dates.map((date, index) => ({ date, value: values[index] }));
  const monthRows = datedValues.filter(row => row.date >= "2026-09-09");
  return {
    start: dates[0],
    end: dates[dates.length - 1],
    portfolio: { metrics: replayMetrics(dates, values), tail: tailRisk(values) },
    spy: { metrics: replayMetrics(dates, spy), tail: tailRisk(spy) },
    oneMonth: replayMetrics(monthRows.map(row => row.date), monthRows.map(row => row.value)),
    oneMonthStart: monthRows[0].date,
    endSpyWeight: 0.6 * spy[spy.length - 1] / values[values.length - 1],
    points: dates.map((date, index) => ({
      time: Date.parse(date),
      portfolio: values[index] - 1,
      spy: spy[index] - 1,
    })),
    histogram: histogram(dailyReturns(values)),
    reference: ([95, 99] as const).map(level => {
      const z = normalQuantile(level);
      return { level, daily: z * 0.2 / Math.sqrt(252), monthly: z * 0.2 * Math.sqrt(21 / 252) };
    }),
    constants: { spyStart: 0.6, aggStart: 0.4, baseAmount: 100000 },
  };
}

export type Example = ReturnType<typeof calculateExample>;
