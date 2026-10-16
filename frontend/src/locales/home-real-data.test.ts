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

it.each(LOCALES.map(item => item.value))("#743 copy revision labels each macro interpretation in %s", locale => {
  const h = catalogs[locale].home;
  const m = h.preview.macro;
  const probable = `[${h.how.tiers.probable}]`;
  const speculative = `[${h.how.tiers.speculative}]`;
  const established = `[${h.how.tiers.established}]`;
  const labels = (text: string) => text.match(/\[[^\]]+\]/g) ?? [];
  expect(labels(m.overview)).toEqual([probable]);
  expect(m.anchor.question.endsWith(speculative)).toBe(true);
  expect(labels(m.anchor.paragraphs[0])).toEqual([probable]);
  expect(m.anchor.paragraphs[0].endsWith(probable)).toBe(true);
  expect(labels(m.anchor.paragraphs[1])).toEqual([probable, probable]);
  expect(labels(m.anchor.paragraphs[2])).toEqual([probable]);
  expect(m.anchor.paragraphs[2].endsWith(probable)).toBe(true);
  expect(labels(m.anchor.paragraphs[3])).toEqual([speculative, probable]);
  expect(labels(m.anchor.paragraphs[4])).toEqual([speculative, speculative]);
  expect(labels(m.updates[0].body)).toEqual([probable]);
  expect(labels(m.updates[1].body)).toEqual([established]);
  // Each transmission / conditional sentence has its own label, rather than
  // sharing the one originally placed at the end of the paragraph.
  const sentenceBoundary = locale.startsWith("zh") ? "。" : ".";
  expect(m.anchor.paragraphs[1]).toContain(`${sentenceBoundary}${locale.startsWith("zh") ? "" : " "}${probable}${locale.startsWith("zh") ? "" : " "}BOXX`);
  expect(m.anchor.paragraphs[4]).toContain(`${sentenceBoundary}${locale.startsWith("zh") ? "" : " "}${speculative}`);
});

const ANALYSIS_HEADINGS = {
  en: [
    "TSMC — 21.7% of the portfolio, the single largest position",
    "Microsoft — 8.3% of the portfolio",
  ],
  es: [
    "TSMC: 21.7% de la cartera, la mayor posición individual",
    "Microsoft: 8.3% de la cartera",
  ],
  "zh-Hans": [
    "台积电 — 占组合 21.7%，第一大单一持仓",
    "微软 — 占组合 8.3%",
  ],
  "zh-Hant": [
    "台積電 — 占組合 21.7%，最大單一持倉",
    "微軟 — 占組合 8.3%",
  ],
} as const;

const EN_ANALYSIS_PARAGRAPHS = [
  [
    "TSMC is the largest position at 21.7%, above the 20% single-stock high-concentration threshold. Its +7.64% rise on 07-30 occurred after Microsoft's earnings release; the timing alone does not establish new orders or a TSMC-specific catalyst.",
    "The company-specific background predates this window: on 07-16, TSMC attributed Q2 performance to demand for leading-edge processes, which accounted for 77% of wafer revenue at 7nm and below. [Established] The documented business channel is demand for those processes reaching TSMC through wafer production; Microsoft's cloud growth does not quantify its share of that demand. [Probable]",
    "Expansion also has a cash and margin dimension. The earlier Q2 disclosures show operating cash generation alongside factory investment, while management's Q3 gross-margin guidance of 65-67% is below Q2's 67.7%, despite higher revenue guidance. [Established] More sales therefore do not establish a uniform improvement in production economics. [Probable] Next observables are the revenue mix, factory spending and gross margin in subsequent disclosures; the guidance is not an achieved result.",
  ],
  [
    "Microsoft's 8.3% position rose +15.51% on 07-30 after its 07-29 fiscal Q4 release. Revenue was $90.01B; Azure and other cloud services grew 43% year on year. [Established] This is a documented operating development alongside the price move, rather than an explanation inferred from the price chart.",
    "The business channel is cloud-service demand reaching Microsoft's revenue: Microsoft Cloud generated $59.3B, up 27% year on year. Commercial remaining performance obligations of $678B describe contracted revenue still to be recognized, not revenue already earned this quarter. [Established] Realized cloud revenue and the conversion of that contracted balance are distinct observables; the backlog alone does not settle the pace of future growth. [Probable]",
    "Capacity expansion has a cash counterpart: quarterly cash additions to property and equipment were $35.8B, versus $17.1B a year earlier. [Established] This is not a measure of AI spending alone or of investment returns. The next disclosures can show cloud growth alongside infrastructure spending and cash generation; that comparison tests how demand and investment are developing together. [Probable] Microsoft's smaller weight than TSMC's gives the direct position a different portfolio scale, without implying independence between the two businesses.",
  ],
] as const;

const ANALYSIS_LABEL_KEYS = [
  [[], ["established", "probable"], ["established", "probable"]],
  [["established"], ["established", "probable"], ["established", "probable"]],
] as const;

it.each(LOCALES.map(item => item.value))("#750 %s keeps two holdings and replaces the single body with three paragraphs", locale => {
  const home = catalogs[locale].home;
  const items = home.preview.analysisItems;
  expect(items).toHaveLength(2);
  expect(items.map(item => item.heading)).toEqual([...ANALYSIS_HEADINGS[locale]]);
  items.forEach((item, index) => {
    expect(item).not.toHaveProperty("body");
    expect(item.paragraphs).toHaveLength(3);
    item.paragraphs.forEach(paragraph => expect(paragraph.trim()).not.toBe(""));
    const labels = item.paragraphs.map(paragraph => paragraph.match(/\[[^\]]+\]/g) ?? []);
    const expected = ANALYSIS_LABEL_KEYS[index].map(keys => keys.map(key => `[${home.how.tiers[key]}]`));
    expect(labels).toEqual(expected);
  });
  const tsmc = items[0].paragraphs.join("\n");
  const microsoft = items[1].paragraphs.join("\n");
  for (const figure of ["21.7%", "20%", "+7.64%", "07-30", "07-16", "77%", "65-67%", "67.7%"]) {
    expect(tsmc).toContain(figure);
  }
  for (const figure of ["8.3%", "+15.51%", "07-29", "07-30", "$90.01B", "43%", "$59.3B", "27%", "$678B", "$35.8B", "$17.1B"]) {
    expect(microsoft).toContain(figure);
  }
  expect(items[0].paragraphs[0]).not.toContain("07-16");
  expect(items[0].paragraphs[1]).toContain("07-16");
  expect(microsoft).not.toContain("60.2%");
  expect(tsmc).not.toMatch(/section 2|sección 2|第二节|第二節/);
  if (locale === "en") {
    expect(items.map(item => item.paragraphs)).toEqual(EN_ANALYSIS_PARAGRAPHS.map(paragraphs => [...paragraphs]));
  }
});
