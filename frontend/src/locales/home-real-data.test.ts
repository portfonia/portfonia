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

it("#743 uses the owner-approved Simplified Chinese wording", () => {
  const h = catalogs["zh-Hans"].home;
  expect(h.preview.macro.overview).toContain("Hammack、Kashkari 和 Logan 主张加息 25 个基点");
  expect(h.preview.macro.overview).toContain("次日公布的 Q2 增速低于 Q1");
  expect(h.preview.calendarRows[0][3]).toBe("制造业动能；价格指数");
  expect(h.faq.items[2].a).toBe("美股、英股、欧股、日股、韩股、港股、A股、中国公募基金，以及现金与外汇。");
  expect(h.productPreviews.startCtaBody).toBe("你已经看到示例简报如何把事件与组合联系起来。录入你的持仓，获取基于你自己持仓的简报。");
});

it("#743 ships the hand-authored Traditional Chinese wording", () => {
  const h = catalogs["zh-Hant"].home;
  expect(h.preview.macro.overview).toContain("Hammack、Kashkari 與 Logan 主張升息 25 個基點");
  expect(h.preview.macro.overview).toContain("隔日公布的 Q2 成長率低於 Q1");
  expect(h.preview.calendarRows[0][3]).toBe("製造業動能；價格指數");
  expect(h.faq.items[2].a).toBe("美股、英股、歐股、日股、韓股、港股、A股、中國公募基金，以及現金與外匯。");
  expect(h.productPreviews.startCtaBody).toBe("你已經看到範例簡報如何連結事件與組合。輸入你的持倉，取得以你自己的持倉為基礎的簡報。");
});
