import { expect, it } from "vitest";
import { catalogs, LOCALES } from "./index";

const anomalyCells = [
  ["+18.99%", "+18.14%", "+21.75%", "+15.51% (2026-07-30)", "450.25", "449.15 (-0.2%)", "448.49-465.96", "463.85", "—"],
  ["+7.89%", "+3.04%", "+0.21%", "+7.64% (2026-07-30)", "402.23", "418.38 (+4.0%)", "397.73-419.23", "403.17", "—"],
  ["+5.77%", "+6.72%", "+11.38%", "+6.73% (2026-07-31)", "333.45", "340.61 (+2.1%)", "339.78-358.35", "355.9", "—"],
];
const technicalCells = [
  ["-4.9%", "+13.6%", "71%", "+49.0%"],
  ["+16.4%", "+7.5%", "60%", "+60.2%"],
  ["-0.7%", "+9.3%", "78%", "+46.1%"],
  ["-3.7%", "+6.9%", "70%", "+23.8%"],
  ["+0.4%", "+7.1%", "93%", "+12.6%"],
  ["+0.4%", "+1.6%", "100%", "+0.3%"],
  ["-1.4%", "-12.0%", "11%", "+24.2%"],
];
function strings(value: unknown): string[] {
  if (typeof value === "string") return [value];
  return value && typeof value === "object" ? Object.values(value).flatMap(strings) : [];
}

it.each(LOCALES.map(item => item.value))("#741 %s snapshot, anomalies, technicals and calendar match the contract", locale => {
  const p = catalogs[locale].home.preview;
  expect(p.totalLine).toContain("968,326");
  expect(p.totalLine).toContain("2026-07-31");
  expect(p.holdingsRows).toHaveLength(12);
  expect(p.holdingsRows[9][0]).toContain("PSH.L");
  expect(p.holdingsRows[9][1]).toBe("GBP");
  const snapshotCells = [
    ["209,648", "21.7%"], ["80,245", "8.3%"], ["69,045", "7.1%"], ["**358,938**", "**37.1%**"],
    ["160,822", "16.6%"], ["130,814", "13.5%"], ["47,904", "4.9%"], ["2,130", "0.2%"],
    ["**341,670**", "**35.3%**"], ["108,713", "15.1%"], ["**146,338**", "**15.1%**"], ["121,380", "12.5%"],
  ];
  expect(p.holdingsRows.map(row => row.slice(2, 4))).toEqual(snapshotCells);
  for (const cell of p.holdingsRows.flat()) expect(cell).not.toMatch(/HK|HKD|CNH|\.SS|0700|1211|518880|510300/);
  expect(p.anomalyColumns).toHaveLength(11);
  expect(p.anomalyRows.map(row => row.slice(1, 10))).toEqual(anomalyCells);
  expect(p.anomalyRows.map(row => row[0].match(/\(([^)]+)\)/)?.[1])).toEqual(["MSFT", "TSM", "GOOGL"]);
  expect(p.anomalyBullets).toHaveLength(3);
  expect(p.technicalRows.map(row => row.slice(1))).toEqual(technicalCells);
  expect(p.technicalRows.map(row => row[0].match(/\(([^)]+)\)/)?.[1])).toEqual(["TSM", "MSFT", "GOOGL", "QQQ", "VOO", "BOXX", "PSH.L"]);
  expect(p.calendarRows.map(row => row[0])).toEqual(["08-03", "08-06", "08-07"]);
});

it.each(LOCALES.map(item => item.value))("#741 %s structured copy, honest provenance and supported markets", locale => {
  const c = catalogs[locale]; const p = c.home.preview;
  expect(p).not.toHaveProperty("macroBody");
  expect(p).not.toHaveProperty("analysisHeading");
  expect(p).not.toHaveProperty("analysisBody");
  expect(p.macro.overview.trim()).not.toBe("");
  expect(p.macro.anchor.question.trim()).not.toBe("");
  expect(p.macro.anchor.paragraphs.length).toBeGreaterThanOrEqual(3);
  expect(p.macro.anchor.paragraphs.length).toBeLessThanOrEqual(5);
  expect(p.macro.updates).toHaveLength(2);
  expect(p.analysisItems).toHaveLength(2);
  expect(p.analysisItems[0].heading).toContain("21.7%");
  expect(p.analysisItems[1].heading).toContain("8.3%");
  expect(c.home.productPreviews.metricValue).toBe("+9.84%");
  expect(c.home.productPreviews.markets).toHaveProperty("uk");
  expect(Object.keys(c.home.productPreviews.markets)).toEqual(["us", "uk"]);
  expect(c.home.productPreviews.assetClasses).toHaveProperty("cash");
  expect(c.home.productPreviews.groups).toHaveProperty("compounding");
  for (const text of strings([c.home, c.seo.og])) {
    expect(text).not.toContain("CSI 300");
    expect(text).not.toContain(c.portfolio.performance.benchmarkNames.csi300);
    expect(text).not.toMatch(/anonymized|real report|informe real|真实报告|真實報告|脱敏|去識別化/i);
  }
  expect(c.seo.og.markets).toHaveLength(7);
  expect(c.home.how.cards[2].tags).toHaveLength(2);
});

it("#741 revised English prose stays within the layer-3 vocabulary", () => {
  const h = catalogs.en.home;
  const revised = [h.preview.macro, h.preview.analysisItems, h.preview.anomalyBullets, h.preview.concentrationBody,
    h.productPreviews.startCtaHeading, h.productPreviews.startCtaBody, h.hero.sub, h.how.cards];
  const forbidden = /\b(?:recommend|should|buy|sell|hold|reduce|increase|exit|stop-loss|target price|will (?:rise|fall) to|entry point|oversold|overbought|strong buy|(?:bullish|bearish) rating)\b/i;
  for (const text of strings(revised)) expect(text).not.toMatch(forbidden);
});

it("#741 Spanish tracking title fits the OG input card on one line", () => {
  expect(catalogs.es.home.how.cards[1].title).toBe("Mercados y macro");
});
