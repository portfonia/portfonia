import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { catalogs, type Locale } from "@/locales";
import { ASSET_CLASS_COLORS } from "@/app/portfolio/performance/_components/allocation-data";
import { getPortfolioPerformance, getPortfolioRisk, getPortfolioSummary } from "@/lib/api";
import { LocaleProvider } from "./locale-provider";
import { HomeSections } from "./home-sections";
import {
  HOME_ASSET_CLASS_SHARES,
  HOME_GROUP_SHARES,
  HOME_MARKET_SHARES,
} from "./home-product-previews";
import { HOME_RISK_SAMPLE } from "./home-risk-sample";

vi.mock("@/lib/api", () => ({
  getPortfolioSummary: vi.fn(),
  getPortfolioPerformance: vi.fn(),
  getPortfolioRisk: vi.fn(),
}));

const LOCALES: Locale[] = ["en", "zh-Hans", "zh-Hant"];

class SizedResizeObserver {
  private callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) { this.callback = callback; }
  observe(target: Element): void {
    this.callback([{
      target,
      contentRect: { x: 0, y: 0, width: 800, height: 300, top: 0, right: 800, bottom: 300, left: 0, toJSON: () => ({}) },
    } as ResizeObserverEntry], this as unknown as ResizeObserver);
  }
  unobserve(): void {}
  disconnect(): void {}
}
if (typeof globalThis.ResizeObserver !== "undefined") {
  (globalThis as { ResizeObserver: unknown }).ResizeObserver = SizedResizeObserver;
}

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

function cssColor(value: string): string {
  const probe = document.createElement("span");
  probe.style.backgroundColor = value;
  return probe.style.backgroundColor;
}

function legendPairs(root: HTMLElement): { label: string; color: string }[] {
  return [...root.querySelectorAll("ul li")].map((item) => ({
    label: item.textContent?.trim() ?? "",
    color: (item.querySelector("span") as HTMLElement | null)?.style.backgroundColor ?? "",
  }));
}

function shareTotal(shares: Record<string, string>): number {
  return Object.values(shares).reduce((sum, value) => sum + Number(value), 0);
}

function withLocaleStorage(initial?: string) {
  const store = new Map<string, string>();
  if (initial) store.set("portfonia:locale", initial);
  Object.defineProperty(window, "localStorage", {
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
    },
    configurable: true,
  });
}

interface PassedSeries {
  key: string;
  label: string;
  color: string;
  isPortfolio: boolean;
  connectNulls: boolean;
}

interface PassedPoint {
  date: string;
  portfolio: number | null;
}

describe("HomeSections product previews (issue #549)", () => {
  beforeEach(() => {
    vi.mocked(getPortfolioSummary).mockClear();
    vi.mocked(getPortfolioPerformance).mockClear();
    withLocaleStorage();
  });

  it("renders the Simplified Chinese hero copy unchanged", async () => {
    withLocaleStorage("zh-Hans");
    const hero = catalogs["zh-Hans"].home.hero;
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(hero.titleLine1);
    });
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(hero.titleAccent);
    expect(screen.getByText(hero.tagline)).toBeInTheDocument();
    expect(screen.getByText(hero.sub)).toBeInTheDocument();
  });

  it("keeps existing section headings in order and inserts previews after the sample report", () => {
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    const heading = (name: string) => screen.getByRole("heading", { level: 2, name });
    const order = [
      catalogs.en.home.how.heading,
      catalogs.en.home.preview.heading,
      catalogs.en.home.productPreviews.portfolioHeading,
      catalogs.en.home.productPreviews.performanceHeading,
      catalogs.en.home.productPreviews.ctaHeading,
      catalogs.en.home.audience.heading,
      catalogs.en.home.boundary.heading,
      catalogs.en.home.faq.heading,
    ];
    const nodes = order.map((name) => heading(name));
    for (let i = 0; i < nodes.length - 1; i += 1) {
      expect(nodes[i].compareDocumentPosition(nodes[i + 1])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    }

    const preview = document.getElementById("preview");
    const inserted = document.getElementById("product-previews");
    const audience = document.getElementById("audience");
    const boundary = document.getElementById("boundary");
    const faq = document.getElementById("faq");
    expect(preview).not.toBeNull();
    expect(inserted).not.toBeNull();
    expect(audience).not.toBeNull();
    expect(preview!.compareDocumentPosition(inserted!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(inserted!.compareDocumentPosition(audience!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(audience!.compareDocumentPosition(boundary!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(boundary!.compareDocumentPosition(faq!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.getByText(catalogs.en.home.status)).toBeInTheDocument();
    expect(faq!.compareDocumentPosition(screen.getByText(catalogs.en.home.status))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("renders exactly three portfolio donuts with percentage labels and no currency chart", () => {
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    const titles = [...document.querySelectorAll("[data-slot=card-title]")].map((node) => node.textContent);
    expect(titles).toEqual([
      catalogs.en.home.productPreviews.chartMarket,
      catalogs.en.home.productPreviews.chartAssetClass,
      catalogs.en.home.productPreviews.chartGroup,
    ]);
    expect(titles).not.toContain(catalogs.en.portfolio.chartByCurrency);
    expect(shareTotal(HOME_MARKET_SHARES)).toBe(100);
    expect(shareTotal(HOME_ASSET_CLASS_SHARES)).toBe(100);
    expect(shareTotal(HOME_GROUP_SHARES)).toBe(100);
    for (const shares of [HOME_MARKET_SHARES, HOME_ASSET_CLASS_SHARES, HOME_GROUP_SHARES]) {
      for (const value of Object.values(shares)) {
        expect(screen.getAllByText(`${Number(value)}%`).length).toBeGreaterThan(0);
        expect(screen.queryByText(`(${Number(value).toFixed(1)}%)`)).not.toBeInTheDocument();
      }
    }
  });

  it("passes a year-to-date three-series chart, one monthly benchmark, and the static metric", () => {
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    const performance = screen.getByTestId("home-performance-preview");
    const series = JSON.parse(performance.getAttribute("data-series") ?? "[]") as PassedSeries[];
    const points = JSON.parse(performance.getAttribute("data-points") ?? "[]") as PassedPoint[];
    expect(series).toEqual([
      {
        key: "portfolio",
        label: catalogs.en.portfolio.performance.chartPortfolioLabel,
        color: "var(--chart-1)",
        isPortfolio: true,
        connectNulls: true,
      },
      {
        key: "sp500",
        label: catalogs.en.portfolio.performance.benchmarkNames.sp500,
        color: "var(--chart-2)",
        isPortfolio: false,
        connectNulls: false,
      },
      {
        key: "csi300",
        label: catalogs.en.portfolio.performance.benchmarkNames.csi300,
        color: "var(--chart-csi300)",
        isPortfolio: false,
        connectNulls: false,
      },
    ]);
    const dates = points.map((point) => point.date);
    expect(dates[0]).toBe("2026-01-01");
    expect(dates[dates.length - 1]?.startsWith("2026-")).toBe(true);
    expect([...dates].sort()).toEqual(dates);
    expect(points[points.length - 1]?.portfolio).toBeCloseTo(0.0842, 6);
    expect(legendPairs(performance)).toEqual([
      { label: catalogs.en.portfolio.performance.chartPortfolioLabel, color: "var(--chart-1)" },
      { label: catalogs.en.portfolio.performance.benchmarkNames.sp500, color: "var(--chart-2)" },
      { label: catalogs.en.portfolio.performance.benchmarkNames.csi300, color: "var(--chart-csi300)" },
    ]);
    expect(screen.getByText(catalogs.en.home.productPreviews.metricValue)).toBeInTheDocument();
    expect(screen.getByText(catalogs.en.home.productPreviews.yearToDate)).toBeInTheDocument();
    expect(screen.queryByText(/since inception/i)).not.toBeInTheDocument();
    const performanceBlock = screen.getByRole("heading", { level: 2, name: catalogs.en.home.productPreviews.performanceHeading }).parentElement?.parentElement;
    expect(performanceBlock).toBeDefined();
    expect(within(performanceBlock!).queryByText(/annualized/i)).not.toBeInTheDocument();

    const allocation = screen.getByTestId("home-allocation-preview");
    const assetClasses = (allocation.getAttribute("data-asset-classes") ?? "").split(",");
    const assetClassNames = JSON.parse(
      allocation.getAttribute("data-asset-class-names") ?? "{}",
    ) as Record<string, string>;
    const allocationDates = (allocation.getAttribute("data-dates") ?? "").split(",");
    expect(assetClasses.length).toBeGreaterThan(0);
    for (const assetClass of assetClasses) {
      expect(ASSET_CLASS_COLORS[assetClass]).toBeTruthy();
      expect(assetClassNames[assetClass]).toBe(
        catalogs.en.portfolio.assetClasses[assetClass as keyof typeof catalogs.en.portfolio.assetClasses],
      );
    }
    expect(allocationDates).toEqual(dates);
    expect(legendPairs(allocation)).toEqual(
      assetClasses.map((assetClass) => ({
        label: catalogs.en.portfolio.assetClasses[assetClass as keyof typeof catalogs.en.portfolio.assetClasses],
        color: cssColor(ASSET_CLASS_COLORS[assetClass]),
      })),
    );

    const monthly = screen.getByTestId("home-monthly-preview");
    const monthlyRows = JSON.parse(monthly.getAttribute("data-rows") ?? "[]") as {
      benchmark: number | null;
    }[];
    expect(monthly.getAttribute("data-benchmark-name")).toBe(
      catalogs.en.portfolio.performance.benchmarkNames.sp500,
    );
    expect(monthly.getAttribute("data-benchmark-color")).toBe("var(--chart-2)");
    expect(monthlyRows.length).toBeGreaterThan(0);
    expect(monthlyRows.every((row) => "benchmark" in row)).toBe(true);
    expect(monthly.getAttribute("data-rows")).not.toContain("csi300");
    expect(legendPairs(monthly)).toEqual([
      { label: catalogs.en.portfolio.performance.monthlyPortfolioLabel, color: "var(--chart-1)" },
      { label: catalogs.en.portfolio.performance.benchmarkNames.sp500, color: "var(--chart-2)" },
    ]);
  });

  it("adds exactly one Get Started link after performance, aimed at /holdings", () => {
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    const links = screen.getAllByRole("link", { name: catalogs.en.home.hero.ctaPrimary });
    expect(links).toHaveLength(3);
    expect(links.map((link) => link.getAttribute("href"))).toEqual(["/holdings", "/holdings", "/holdings"]);
    const riskPreview = screen.getByTestId("home-risk-preview");
    const portfolioHeading = screen.getByRole("heading", { level: 2, name: catalogs.en.home.productPreviews.portfolioHeading });
    expect(riskPreview.compareDocumentPosition(links[1])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(links[1].compareDocumentPosition(portfolioHeading)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    const performance = screen.getByRole("heading", {
      level: 2,
      name: catalogs.en.home.productPreviews.performanceHeading,
    });
    const audience = document.getElementById("audience");
    expect(performance.compareDocumentPosition(links[2])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(links[2].compareDocumentPosition(audience!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.queryByRole("link", { name: /sign up|waitlist|apply|invite/i })).not.toBeInTheDocument();
  });

  it("does not call portfolio or performance APIs while rendering", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    expect(getPortfolioSummary).not.toHaveBeenCalled();
    expect(getPortfolioPerformance).not.toHaveBeenCalled();
    expect(getPortfolioRisk).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it.each(LOCALES)("ships %s product-preview copy without dropping existing home strings", (locale) => {
    const home = catalogs[locale].home;
    const copy = home.productPreviews;
    expect(copy.portfolioHeading.length).toBeGreaterThan(0);
    expect(copy.portfolioBody.length).toBeGreaterThan(0);
    expect(copy.performanceHeading.length).toBeGreaterThan(0);
    expect(copy.performanceBody.length).toBeGreaterThan(0);
    expect(copy.metricLabel.length).toBeGreaterThan(0);
    expect(copy.metricValue).toBe("+8.42%");
    expect(copy.yearToDate.length).toBeGreaterThan(0);
    expect(copy.chartMarket.length).toBeGreaterThan(0);
    expect(copy.chartAssetClass.length).toBeGreaterThan(0);
    expect(copy.chartGroup.length).toBeGreaterThan(0);
    expect(copy.ctaHeading.length).toBeGreaterThan(0);
    expect(copy.ctaBody.length).toBeGreaterThan(0);
    expect(Object.values(copy.markets)).toHaveLength(4);
    expect(Object.values(copy.assetClasses)).toHaveLength(5);
    expect(Object.values(copy.groups)).toHaveLength(3);
    expect(home.hero.ctaPrimary.length).toBeGreaterThan(0);
    expect(home.hero.titleAccent.length).toBeGreaterThan(0);
  });

  it("places the shared risk preview and its get-started block before portfolio overview", () => {
    const { container } = render(<LocaleProvider><HomeSections /></LocaleProvider>);
    const risk = screen.getByTestId("home-risk-preview");
    const riskHeading = screen.getByRole("heading", { level: 2, name: "Portfolio risk" });
    const riskCta = screen.getByRole("heading", { level: 2, name: "Lasting results come from keeping risk in check" });
    const overview = screen.getByRole("heading", { level: 2, name: catalogs.en.home.productPreviews.portfolioHeading });
    const performance = screen.getByRole("heading", { level: 2, name: catalogs.en.home.productPreviews.performanceHeading });
    const finalCta = screen.getByRole("heading", { level: 2, name: catalogs.en.home.productPreviews.ctaHeading });
    expect(riskHeading.compareDocumentPosition(risk)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(risk.compareDocumentPosition(riskCta)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(riskCta.compareDocumentPosition(overview)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(overview.compareDocumentPosition(performance)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(performance.compareDocumentPosition(finalCta)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.getByText("Sample figures, not your holdings. The same illustrative portfolio's historical volatility, Beta against the S&P 500, and where it sits against a personal questionnaire.")).toBeInTheDocument();
    expect(screen.getByText("Returns decide how fast you go; risk decides how far. See how much your portfolio swings, how closely it moves with the market, and how far it sits from the tolerance you set.")).toBeInTheDocument();
    expect(container.querySelectorAll('[data-testid="home-risk-preview"]')).toHaveLength(1);
  });

  it("renders the static shared visuals without account disclosures or a selector", async () => {
    const { container } = render(<LocaleProvider><HomeSections /></LocaleProvider>);
    const risk = screen.getByTestId("home-risk-preview");
    const beta = within(risk).getByRole("button", { name: /Beta:/ });
    expect(beta).toHaveTextContent("1.12");
    expect(beta.querySelector('[data-testid="beta-portfolio-marker"]')).not.toBeNull();
    expect(beta.querySelector('[data-testid="beta-reference-marker"]')).not.toBeNull();
    expect(within(risk).getByRole("button", { name: /^Risk:/ })).toHaveTextContent("Caution");
    expect(within(risk).getByRole("button", { name: /^Deviation:/ })).toHaveTextContent("Aggressive direction +1 tier");
    for (const value of ["18.6%", "14.2%", "16.8%", catalogs.en.portfolio.riskPanel.footnote]) {
      expect(within(risk).getByText(value)).toBeInTheDocument();
    }
    expect(within(risk).queryByRole("button", { name: /Volatility comparison benchmark/i })).toBeNull();
    fireEvent.focus(beta);
    expect(within(risk).getByRole("tooltip")).toHaveTextContent(catalogs.en.portfolio.riskPanel.betaExplanation);
    fireEvent.blur(beta);
    expect(risk.textContent).not.toContain("Approximate TWR");
    expect(risk.textContent).not.toContain("Manual valuation");
    await waitFor(() => expect(risk.querySelectorAll("path.recharts-line-curve")).toHaveLength(3));
    expect(container.querySelector('[data-testid="home-risk-preview"]')).toBe(risk);
  });

  it("keeps the sample literal coherent and ships the approved risk copy", () => {
    expect(HOME_RISK_SAMPLE.beta.value).toBe("1.12");
    expect(HOME_RISK_SAMPLE.risk.label).toBe("caution");
    expect(HOME_RISK_SAMPLE.deviation.delta).toBe(1);
    expect(HOME_RISK_SAMPLE.portfolio_vol.current).toBe("0.186");
    expect(HOME_RISK_SAMPLE.benchmark_vols.map((item) => [item.code, item.current])).toEqual([["sp500", "0.142"], ["csi300", "0.168"]]);
    for (const series of [HOME_RISK_SAMPLE.portfolio_vol, ...HOME_RISK_SAMPLE.benchmark_vols]) {
      expect(series.points.length).toBeGreaterThanOrEqual(35);
      expect(series.points.at(-1)).toEqual({ date: "2026-09-18", vol: series.current });
      expect(series.window_start).toBe(series.points[0].date);
      expect(series.window_end).toBe("2026-09-18");
    }
    expect(catalogs.en.home.productPreviews.riskHeading).toBe("Portfolio risk");
    expect(catalogs.en.home.productPreviews.riskCtaHeading).toBe("Lasting results come from keeping risk in check");
    expect(catalogs["zh-Hans"].home.productPreviews.riskHeading).toBe("风险画像");
    expect(catalogs["zh-Hans"].home.productPreviews.riskBody).toBe("示例数字，不是你的持仓。同一份示例组合的历史波动、相对 S&P 500 的 Beta，以及对照个人问卷的风险位置与偏离度。");
    expect(catalogs["zh-Hans"].home.productPreviews.riskCtaHeading).toBe("走得长远，靠的是管住风险");
    expect(catalogs["zh-Hans"].home.productPreviews.riskCtaBody).toBe("收益决定你能走多快，风险决定你能走多远。先看清组合的波动有多大、和大盘有多同步、离自己设定的承受范围有多远。");
    for (const locale of LOCALES) {
      const copy = catalogs[locale].home.productPreviews;
      for (const key of ["riskHeading", "riskBody", "riskCtaHeading", "riskCtaBody"] as const) expect(copy[key]).toBeTruthy();
    }
  });
});
