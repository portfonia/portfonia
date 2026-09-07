import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { catalogs, type Locale } from "@/locales";
import { LocaleProvider } from "./locale-provider";
import { HomeSections } from "./home-sections";

const LOCALES: Locale[] = ["en", "zh-Hans", "zh-Hant"];

function parseUsdAmount(cell: string): number {
  return Number(cell.replace(/\*\*/g, "").replace(/,/g, ""));
}

function parsePercent(cell: string): number {
  return Number(cell.replace(/\*\*/g, "").replace("%", ""));
}

// Every locale's sample data names its institutions "<word> A".."<word> E"
// in the institution column (index 4) — e.g. "Custodian A" / "机构 A" /
// "機構 A". Deriving the localized word from holding A's own row (instead of
// hardcoding all three locales' translations here) keeps this test locale-
// agnostic.
function institutionWord(holdingsRows: string[][]): string {
  const [firstRow] = holdingsRows;
  return firstRow[4].replace(/ A$/, "");
}

function institutionRow(holdingsRows: string[][], word: string, letter: string): string[] {
  const label = `${word} ${letter}`;
  const row = holdingsRows.find((r) => r[4] === label);
  if (!row) throw new Error(`no holdingsRow with institution "${label}"`);
  return row;
}

// Subtotal rows carry the institution word + letter as a substring of the
// label column (index 0), e.g. "**Custodian D subtotal**" / "**机构 D 小计**".
function subtotalRow(subtotalRows: string[][], word: string, letter: string): string[] {
  const needle = `${word} ${letter}`;
  const row = subtotalRows.find((r) => r[0].includes(needle));
  if (!row) throw new Error(`no subtotal row containing "${needle}"`);
  return row;
}

// USD/CNH/HKD columns render as a localized currency NAME, not the ISO code,
// in the two Chinese locales (pre-existing behavior, not introduced by
// issue #209) — one set of name variants per code, checked per locale.
const CURRENCY_NAMES: Record<string, string[]> = {
  USD: ["USD", "美元"],
  CNH: ["CNH", "人民币", "人民幣"],
  HKD: ["HKD", "港元", "港幣"],
};

function isCurrency(cell: string, code: keyof typeof CURRENCY_NAMES): boolean {
  return CURRENCY_NAMES[code].includes(cell);
}

// The sample-briefing section must show EVERY report section (owner ask,
// issue #207 follow-up): snapshot, macro signals, forward calendar, holding
// analysis, risk radar — plus the subscription-tier footnote.
describe("HomeSections sample briefing", () => {
  it("renders every report section in the anonymized real-report sample", () => {
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    expect(screen.getByText("Portfolio snapshot")).toBeInTheDocument();
    expect(screen.getByText("Macro signals")).toBeInTheDocument();
    expect(screen.getByText("Forward calendar")).toBeInTheDocument();
    expect(screen.getByText("Holding analysis")).toBeInTheDocument();
    expect(screen.getByText("Risk radar")).toBeInTheDocument();
    expect(
      screen.getByText(/varies depending on your subscription tier/i),
    ).toBeInTheDocument();
  });

  it("carries the MVP closed-beta status line instead of Ring 0", () => {
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    expect(screen.getByText(/MVP — multi-user closed beta/i)).toBeInTheDocument();
    expect(screen.queryByText(/Ring 0/)).not.toBeInTheDocument();
    // The hero eyebrow renders the same MVP wording.
    expect(screen.getAllByText(/multi-user closed beta/i).length).toBeGreaterThanOrEqual(2);
  });

  it("renders bold markers in snapshot table cells as emphasis, never literal asterisks", () => {
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    // Subtotal rows carry ** markers in label, value AND weight columns —
    // all must render as styled text, none as raw asterisks.
    for (const text of ["358,000", "35.8%", "341,000", "34.1%"]) {
      const strongs = screen.getAllByText((_, element) => element?.tagName === "STRONG" && element.textContent === text);
      expect(strongs.length).toBeGreaterThan(0);
    }
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument();
  });

  it.each(LOCALES)(
    "keeps %s sample snapshot internally consistent (USD subtotals = total, weights = 100)",
    (locale) => {
      const preview = catalogs[locale].home.preview;
      const totalMatch = preview.totalLine.match(/[\d,]+/);
      expect(totalMatch).not.toBeNull();
      const advertisedTotal = Number(totalMatch![0].replace(/,/g, ""));

      const word = institutionWord(preview.holdingsRows);
      const usdSubtotals = preview.holdingsRows.filter(
        (row) => row[0].includes("subtotal") || row[0].includes("小计") || row[0].includes("小計"),
      );
      const subtotalSum = usdSubtotals.reduce(
        (sum, row) => sum + parseUsdAmount(row[2]),
        0,
      );
      const eRow = institutionRow(preview.holdingsRows, word, "E");
      expect(subtotalSum + parseUsdAmount(eRow[2])).toBe(advertisedTotal);

      const weightSum =
        usdSubtotals.reduce((sum, row) => sum + parsePercent(row[3]), 0) +
        parsePercent(eRow[3]);
      expect(weightSum).toBeCloseTo(100, 5);

      const dSubtotalRow = subtotalRow(usdSubtotals, word, "D");
      const cnhUsd = parseUsdAmount(dSubtotalRow[2]);
      const cnhFace = preview.holdingsRows
        .filter((row) => isCurrency(row[1], "CNH"))
        .reduce((sum, row) => sum + parseUsdAmount(row[2]), 0);
      expect(cnhFace / 7.15).toBeCloseTo(cnhUsd, 0);

      const cSubtotalRow = subtotalRow(usdSubtotals, word, "C");
      const hkdUsd = parseUsdAmount(cSubtotalRow[2]);
      const hkdFace = preview.holdingsRows
        .filter((row) => isCurrency(row[1], "HKD"))
        .reduce((sum, row) => sum + parseUsdAmount(row[2]), 0);
      expect(hkdFace / 7.8).toBeCloseTo(hkdUsd, 0);
    },
  );

  it.each(LOCALES)(
    "ships %s positive-framing keys together (issue #364)",
    (locale) => {
      const home = catalogs[locale].home;
      expect(home.hero.tagline.length).toBeGreaterThan(0);
      expect(home.audience.heading.length).toBeGreaterThan(0);
      expect(home.audience.paragraphs).toHaveLength(3);
      for (const paragraph of home.audience.paragraphs) {
        expect(paragraph.length).toBeGreaterThan(0);
      }
    },
  );

  it("keeps zh-Hans homepage copy verbatim from the 2026-09-06 vault wording (issue #364)", () => {
    const home = catalogs["zh-Hans"].home;
    expect(home.hero.tagline).toBe(
      "以高相关的信息深度与广度，陪你穿越周期，沉淀长久的价值回报。",
    );
    expect(home.hero.sub).toBe(
      "Portfonia 是一份为穿越周期而生的持仓情报服务。我们持续追踪新闻、宏观事件与公司动态，把它们系统地映射到你手中的每一笔持仓，带来与你的关注高度相关的信息深度和广度。周期有涨有落，但值得长久持有的判断力，需要同样值得长久信赖的信息陪伴——这是我们存在的理由。",
    );
    expect(home.audience.paragraphs).toEqual([
      "Portfonia 是一份陪伴投资者穿越周期的持仓情报服务。我们相信，真正有质量的投资决策，建立在对趋势、周期与基本面的深度理解之上，而这份理解需要时间沉淀，也需要与你的持仓高度相关的信息，持续、可靠地支撑。",
      "我们的工作，是把分散在新闻、宏观事件与公司动态里的信息，系统地映射回你的每一笔持仓，带来真正贴合你所关注标的的信息深度与广度，让你随时看清资产所处的位置、正在经历的变化，以及值得留意的信号。你保有完整的判断与决策权，我们负责让这份判断，建立在扎实的信息基础之上。",
      "市场周期有起有落，Portfonia 陪伴的，是每一次跨越周期、经得起时间检验的决定。",
    ]);
  });

  it("does not replace the hero titleAccent slot with the marketing tagline (issue #364)", () => {
    expect(catalogs.en.home.hero.titleAccent).toBe("in your holdings.");
    expect(catalogs.en.home.hero.tagline).not.toBe(catalogs.en.home.hero.titleAccent);
    expect(catalogs["zh-Hans"].home.hero.titleAccent).toBe("就藏在你的持仓里。");
    expect(catalogs["zh-Hant"].home.hero.titleAccent).toBe("就藏在你的持倉裡。");
  });

  it("leaves Layer-3 compliance boundary copy unchanged (issue #364)", () => {
    expect(catalogs.en.home.boundary).toEqual({
      heading: "Deliberately out of scope",
      body: "Portfonia is an intelligence service, not an advisory one. The boundary is enforced at the template and prompt layer — not left to the model's judgment.",
      items: [
        "No buy / sell / hold / reduce / increase / target-price language. Ever.",
        "No trade execution, no broker integrations beyond ingest-only.",
        "No tax or capital-gains computation, no P&L from trade history.",
        "No options, futures, or derivatives.",
        "No threshold price alerts — every broker app already does that.",
        "No social or sharing features — holdings are sensitive data.",
      ],
    });
    expect(catalogs["zh-Hans"].home.boundary).toEqual({
      heading: "明确不做的事",
      body: "Portfonia 是情报服务，不是投顾服务。这条边界在模板和 prompt 层强制执行——不依赖模型自己判断。",
      items: [
        "不出现买入 / 卖出 / 持有 / 减仓 / 加仓 / 目标价这类措辞。绝不会有。",
        "不做交易执行，除录入外不对接券商。",
        "不做税务或资本利得计算，不基于交易记录算盈亏。",
        "不涉及期权、期货或衍生品。",
        "不做阈值价格提醒（如“跌了 5%”）——每个券商 App 都已经在做这件事。",
        "早期阶段不做社交或分享功能——持仓是敏感数据。",
      ],
    });
    expect(catalogs["zh-Hant"].home.boundary).toEqual({
      heading: "刻意不做的事",
      body: "Portfonia 是情報服務，不是投顧服務。這條界線在範本與 prompt 層強制執行——不依賴模型自行判斷。",
      items: [
        "不出現買入 / 賣出 / 持有 / 減碼 / 加碼 / 目標價這類措辭。絕不會有。",
        "不執行交易，除匯入功能外不對接券商。",
        "不做稅務或資本利得計算，不根據交易紀錄計算損益。",
        "不涉及選擇權、期貨或衍生性金融商品。",
        "不做門檻價格提醒（如「跌了 5%」）——每個券商 App 都已經在做這件事。",
        "現階段不做社交或分享功能——持倉是敏感資料。",
      ],
    });
  });

  it("renders tagline and audience between how and boundary, without a new /about route (issue #364)", () => {
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    expect(screen.getByText(catalogs.en.home.hero.tagline)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: catalogs.en.home.audience.heading })).toBeInTheDocument();
    for (const paragraph of catalogs.en.home.audience.paragraphs) {
      expect(screen.getByText(paragraph)).toBeInTheDocument();
    }

    const how = document.getElementById("how");
    const audience = document.getElementById("audience");
    const boundary = document.getElementById("boundary");
    expect(how).not.toBeNull();
    expect(audience).not.toBeNull();
    expect(boundary).not.toBeNull();
    expect(how!.compareDocumentPosition(audience!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(audience!.compareDocumentPosition(boundary!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.queryByRole("link", { name: /about/i })).not.toBeInTheDocument();
  });

  it.each(LOCALES)(
    "names only snapshot holdings in the %s risk-radar anomaly table",
    (locale) => {
      const preview = catalogs[locale].home.preview;
      const heldTickers = new Set(
        preview.holdingsRows.flatMap((row) => {
          const match = row[0].match(/\(([A-Za-z0-9.]+)\)/);
          return match ? [match[1]] : [];
        }),
      );
      for (const row of preview.anomalyRows) {
        const match = row[0].match(/\(([A-Za-z0-9.]+)\)/);
        expect(match, `anomaly ${row[0]} has no ticker`).not.toBeNull();
        expect(heldTickers.has(match![1]), `${match![1]} missing from snapshot`).toBe(true);
      }
    },
  );
});
