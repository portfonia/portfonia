import expectedData from "./home-real-data.fixture.json";
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
  HOME_PERFORMANCE_ROWS,
  HOME_SERIES_POINTS,
  HOME_MONTHLY_ROWS,
} from "./home-product-previews";
import { HOME_RISK_SAMPLE } from "./home-risk-sample";

vi.mock("@/lib/api", () => ({
  getPortfolioSummary: vi.fn(),
  getPortfolioPerformance: vi.fn(),
  getPortfolioRisk: vi.fn(),
}));

const LOCALES: Locale[] = ["en", "es", "zh-Hans", "zh-Hant"];

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

// The sample-briefing section must show EVERY report section (owner ask,
// issue #207 follow-up): snapshot, macro events, forward calendar, holdings
// context, exposure & price data — plus the subscription-tier footnote.
describe("HomeSections sample briefing", () => {
  it("renders every report section in the real-market-data sample", () => {
    render(
      <LocaleProvider routeLocale={null}>
        <HomeSections />
      </LocaleProvider>,
    );

    expect(screen.getByText("Portfolio snapshot")).toBeInTheDocument();
    expect(screen.getByText("Macro events")).toBeInTheDocument();
    expect(screen.getByText("Forward calendar")).toBeInTheDocument();
    expect(screen.getByText("Holdings context")).toBeInTheDocument();
    expect(screen.getByText("Exposure & price data")).toBeInTheDocument();
    expect(screen.getByText("Briefing frequency depends on your plan.")).toBeInTheDocument();
  });

  it("renders the limited public service status and hero eyebrow", () => {
    render(
      <LocaleProvider routeLocale={null}>
        <HomeSections />
      </LocaleProvider>,
    );

    expect(screen.getByText(/^Limited public service\. Portfonia maps/)).toBeInTheDocument();
    expect(screen.getByText("Limited public service")).toBeInTheDocument();
  });

  it("renders bold markers in snapshot table cells as emphasis, never literal asterisks", () => {
    render(
      <LocaleProvider routeLocale={null}>
        <HomeSections />
      </LocaleProvider>,
    );

    // Subtotal rows carry ** markers in label, value AND weight columns —
    // all must render as styled text, none as raw asterisks.
    for (const text of ["358,938", "37.1%", "341,670", "35.3%"]) {
      const strongs = screen.getAllByText((_, element) => element?.tagName === "STRONG" && element.textContent === text);
      expect(strongs.length).toBeGreaterThan(0);
    }
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument();
  });

  it.each(LOCALES)("keeps %s USD subtotals and cash consistent with the total", (locale) => {
    const preview = catalogs[locale].home.preview;
    expect(preview.totalLine).toContain("968,326");
    const rows = preview.holdingsRows;
    expect([rows[3], rows[8], rows[10], rows[11]].reduce((sum, row) => sum + parseUsdAmount(row[2]), 0)).toBe(968326);
  });

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

  it("locks current zh-Hans hero and audience copy with the software-tool wording (issue #615)", () => {
    const home = catalogs["zh-Hans"].home;
    expect(home.hero.tagline).toBe(
      "以高相关的信息深度与广度，陪你穿越周期，沉淀长久的价值回报。",
    );
    expect(home.hero.sub).toBe(
      "Portfonia 是一款软件工具，持续追踪新闻、宏观事件与公司动态，并将这些信息映射到你录入的持仓。",
    );
    expect(home.audience.paragraphs).toEqual([
      "Portfonia 是一款面向穿越周期的投资者的软件工具。深入理解趋势、周期与基本面需要时间，也需要与你的持仓紧密相关的信息，持续、可靠地送达。",
      "我们的工作，是把分散在新闻、宏观事件与公司动态里的信息，系统地映射回你的每一笔持仓，带来真正贴合你所关注标的的信息深度与广度，让你随时看清资产所处的位置、正在经历的变化，以及值得留意的信号。你保有完整的判断与决策权，我们负责让这份判断，建立在扎实的信息基础之上。",
      "市场周期有起有落。Portfonia 在每个周期中持续提供信息。",
    ]);
  });

  it("does not replace the hero titleAccent slot with the marketing tagline (issue #364)", () => {
    expect(catalogs.en.home.hero.titleAccent).toBe("in your holdings.");
    expect(catalogs.en.home.hero.tagline).not.toBe(catalogs.en.home.hero.titleAccent);
    expect(catalogs["zh-Hans"].home.hero.titleAccent).toBe("就藏在你的持仓里。");
    expect(catalogs["zh-Hant"].home.hero.titleAccent).toBe("就藏在你的持倉裡。");
  });

  it("locks the software-tool boundary copy while preserving boundary items (issue #615)", () => {
    expect(catalogs.en.home.boundary).toEqual({
      heading: "Deliberately out of scope",
      body: "Portfonia is a software tool, not an advisory service. The boundary is enforced in code at the template and prompt layer — not left to the model's judgment.",
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
      body: "Portfonia 是软件工具，不是投顾服务。这条边界通过代码在模板和 prompt 层强制执行——不依赖模型自己判断。",
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
      body: "Portfonia 是軟體工具，不是投顧服務。這條界線透過程式碼在範本與 prompt 層強制執行——不依賴模型自行判斷。",
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

  it("renders tagline and audience between hero and how, without a new /about route (issue #364)", () => {
    render(
      <LocaleProvider routeLocale={null}>
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
    expect(audience!.compareDocumentPosition(how!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
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
      <LocaleProvider routeLocale={null}>
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
      <LocaleProvider routeLocale={null}>
        <HomeSections />
      </LocaleProvider>,
    );

    const heading = (name: string) => screen.getByRole("heading", { level: 2, name });
    const order = [
      catalogs.en.home.audience.heading,
      catalogs.en.home.how.heading,
      catalogs.en.home.preview.heading,
      catalogs.en.home.productPreviews.startCtaHeading,
      catalogs.en.home.productPreviews.riskHeading,
      catalogs.en.home.productPreviews.portfolioHeading,
      catalogs.en.home.productPreviews.performanceHeading,
      catalogs.en.home.productPreviews.ctaHeading,
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
    expect(audience!.compareDocumentPosition(inserted!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(audience!.compareDocumentPosition(boundary!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(boundary!.compareDocumentPosition(faq!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.getByText(catalogs.en.home.status)).toBeInTheDocument();
    expect(faq!.compareDocumentPosition(screen.getByText(catalogs.en.home.status))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("renders exactly three portfolio donuts with percentage labels and no currency chart", () => {
    render(
      <LocaleProvider routeLocale={null}>
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
      <LocaleProvider routeLocale={null}>
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
        key: "nasdaq",
        label: catalogs.en.portfolio.performance.benchmarkNames.nasdaq,
        color: "var(--chart-4)",
        isPortfolio: false,
        connectNulls: false,
      },
    ]);
    const dates = points.map((point) => point.date);
    expect(dates[0]).toBe("2026-01-01");
    expect(points).toHaveLength(45);
    expect(dates.at(-1)).toBe("2026-09-30");
    expect(dates.every((date, index) => index === 0 || date > dates[index - 1])).toBe(true);
    expect([...dates].sort()).toEqual(dates);
    expect(points[points.length - 1]?.portfolio).toBeCloseTo(0.0984, 6);
    expect(legendPairs(performance)).toEqual([
      { label: catalogs.en.portfolio.performance.chartPortfolioLabel, color: "var(--chart-1)" },
      { label: catalogs.en.portfolio.performance.benchmarkNames.sp500, color: "var(--chart-2)" },
      { label: catalogs.en.portfolio.performance.benchmarkNames.nasdaq, color: "var(--chart-4)" },
    ]);
    expect(screen.getByText("+9.84%")).toBeInTheDocument();
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

  it("renders five Get Started links to /holdings, with CTAs between risk and overview and after performance", () => {
    render(
      <LocaleProvider routeLocale={null}>
        <HomeSections />
      </LocaleProvider>,
    );

    const links = screen.getAllByRole("link", { name: catalogs.en.home.hero.ctaPrimary });
    expect(links).toHaveLength(5);
    expect(links.map((link) => link.getAttribute("href"))).toEqual(["/holdings", "/holdings", "/holdings", "/holdings", "/holdings"]);
    expect(document.getElementById("jade-tools")).toContainElement(links[4]);
    const riskPreview = screen.getByTestId("home-risk-preview");
    const portfolioHeading = screen.getByRole("heading", { level: 2, name: catalogs.en.home.productPreviews.portfolioHeading });
    expect(riskPreview.compareDocumentPosition(links[2])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(links[2].compareDocumentPosition(portfolioHeading)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    const performance = screen.getByRole("heading", {
      level: 2,
      name: catalogs.en.home.productPreviews.performanceHeading,
    });
    const audience = document.getElementById("audience");
    expect(performance.compareDocumentPosition(links[3])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(audience!.compareDocumentPosition(links[3])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.queryByRole("link", { name: /sign up|waitlist|apply|invite/i })).not.toBeInTheDocument();
  });

  it("does not call portfolio or performance APIs while rendering", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(
      <LocaleProvider routeLocale={null}>
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
    expect(copy.metricValue).toBe("+9.84%");
    expect(copy.yearToDate.length).toBeGreaterThan(0);
    expect(copy.chartMarket.length).toBeGreaterThan(0);
    expect(copy.chartAssetClass.length).toBeGreaterThan(0);
    expect(copy.chartGroup.length).toBeGreaterThan(0);
    expect(copy.ctaHeading.length).toBeGreaterThan(0);
    expect(copy.ctaBody.length).toBeGreaterThan(0);
    expect(Object.values(copy.markets)).toHaveLength(2);
    expect(Object.values(copy.assetClasses)).toHaveLength(5);
    expect(Object.values(copy.groups)).toHaveLength(3);
    expect(home.hero.ctaPrimary.length).toBeGreaterThan(0);
    expect(home.hero.titleAccent.length).toBeGreaterThan(0);
  });

  it("places the shared risk preview and its get-started block before portfolio overview", () => {
    const { container } = render(<LocaleProvider routeLocale={null}><HomeSections /></LocaleProvider>);
    const risk = screen.getByTestId("home-risk-preview");
    const riskHeading = screen.getByRole("heading", { level: 2, name: "Portfolio risk" });
    const riskCta = screen.getByRole("heading", { level: 2, name: "Going the distance means balancing opportunity and risk" });
    const overview = screen.getByRole("heading", { level: 2, name: catalogs.en.home.productPreviews.portfolioHeading });
    const performance = screen.getByRole("heading", { level: 2, name: catalogs.en.home.productPreviews.performanceHeading });
    const finalCta = screen.getByRole("heading", { level: 2, name: catalogs.en.home.productPreviews.ctaHeading });
    expect(riskHeading.compareDocumentPosition(risk)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(risk.compareDocumentPosition(riskCta)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(riskCta.compareDocumentPosition(overview)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(overview.compareDocumentPosition(performance)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(performance.compareDocumentPosition(finalCta)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.getByText(catalogs.en.home.productPreviews.riskBody)).toBeInTheDocument();
    expect(screen.getByText("Opportunity decides how fast you can go; risk decides how steady the ride is. The two reinforce and constrain each other, and you need both. Here you can see how much your portfolio swings, how closely it moves with the market, and how far it sits from the tolerance you set.")).toBeInTheDocument();
    expect(container.querySelectorAll('[data-testid="home-risk-preview"]')).toHaveLength(1);
  });

  it("renders the static shared visuals without account disclosures or a selector", async () => {
    const { container } = render(<LocaleProvider routeLocale={null}><HomeSections /></LocaleProvider>);
    const risk = screen.getByTestId("home-risk-preview");
    const beta = within(risk).getByRole("button", { name: /Beta:/ });
    expect(beta).toHaveTextContent("1.28");
    expect(beta.querySelector('[data-testid="beta-portfolio-marker"]')).not.toBeNull();
    expect(beta.querySelector('[data-testid="beta-reference-marker"]')).not.toBeNull();
    expect(within(risk).getByRole("button", { name: /^Risk:/ })).toHaveTextContent("Caution");
    expect(within(risk).getByRole("button", { name: /^Deviation:/ })).toHaveTextContent("Aggressive direction +1 tier");
    for (const value of ["16.0%", "11.0%", "17.3%", catalogs.en.portfolio.riskPanel.footnote]) {
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
    expect(HOME_RISK_SAMPLE.beta.value).toBe("1.28");
    expect(HOME_RISK_SAMPLE.risk.label).toBe("caution");
    expect(HOME_RISK_SAMPLE.deviation.delta).toBe(1);
    expect(HOME_RISK_SAMPLE.portfolio_vol.current).toBe("0.160");
    expect(HOME_RISK_SAMPLE.benchmark_vols.map((item) => [item.code, item.current])).toEqual([["sp500", "0.110"], ["nasdaq", "0.173"]]);
    for (const series of [HOME_RISK_SAMPLE.portfolio_vol, ...HOME_RISK_SAMPLE.benchmark_vols]) {
      expect(series.points.length).toBe(60);
      expect(series.points.at(-1)).toEqual({ date: "2026-09-18", vol: series.current });
      expect(series.window_start).toBe(series.points[0].date);
      expect(series.window_end).toBe("2026-09-18");
    }
    expect(catalogs.en.home.productPreviews.riskHeading).toBe("Portfolio risk");
    expect(catalogs.en.home.productPreviews.riskCtaHeading).toBe("Going the distance means balancing opportunity and risk");
    expect(catalogs["zh-Hans"].home.productPreviews.riskHeading).toBe("风险画像");
    expect(catalogs["zh-Hans"].home.productPreviews.riskBody).toContain("平衡型");
    expect(catalogs["zh-Hans"].home.productPreviews.riskCtaHeading).toBe("走得长远，需要平衡好机会与风险");
    expect(catalogs["zh-Hans"].home.productPreviews.riskCtaBody).toBe("收益机会决定你能走多快，风险决定你能走多稳。两者相辅相成，又互相制约，缺一不可。在这里看清组合的波动有多大、和大盘有多同步、离自己设定的承受范围有多远。");
    for (const locale of LOCALES) {
      const copy = catalogs[locale].home.productPreviews;
      for (const key of ["riskHeading", "riskBody", "riskCtaHeading", "riskCtaBody"] as const) expect(copy[key]).toBeTruthy();
    }
  });
});


it.each(LOCALES)("issue #702 shows the %s product image before the sample report", (locale) => {
  const { container } = render(<LocaleProvider routeLocale={locale}><HomeSections /></LocaleProvider>);
  const preview = container.querySelector("#preview");
  const how = container.querySelector("#how");
  const image = how?.querySelector("img");
  expect(preview?.querySelector("img")).toBeNull();
  expect(image).toHaveAttribute("src", `/og/${locale}`);
  expect(image).toHaveAttribute("alt", catalogs[locale].seo.ogImageAlt);
  expect(image).toHaveAttribute("width", "1200");
  expect(image).toHaveAttribute("height", "630");
  const report = preview?.querySelector(".border-dashed");
  expect(image && report && image.compareDocumentPosition(report) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const prefix = locale === "en" ? "" : `/${locale}`;
  for (const path of ["pricing", "terms", "privacy", "refund"]) {
    expect(container.querySelector(`a[href="${prefix}/${path}"]`), path).not.toBeNull();
  }
});


it("renders crawlable language choices inside the home footer", () => {
  const { container } = render(<LocaleProvider routeLocale="zh-Hans"><HomeSections /></LocaleProvider>);
  expect(container.querySelector('footer a[href="/zh-Hant"]')).not.toBeNull();
});


it("#706 compounds nine complete monthly returns to the cumulative portfolio and S&P 500", () => {
  expect(HOME_MONTHLY_ROWS).toHaveLength(9);
  const last = HOME_PERFORMANCE_ROWS.at(-1);
  for (const [monthlyKey, cumulativeKey] of [["portfolio", "portfolio"], ["benchmark", "sp500"]] as const) {
    const cumulative = HOME_MONTHLY_ROWS.reduce((value, row) => value * (1 + (row[monthlyKey] ?? 0)), 1) - 1;
    expect(Math.abs(cumulative - Number(last?.[cumulativeKey]))).toBeLessThan(0.0005);
  }
  expect(HOME_MONTHLY_ROWS.every(row => row.partialReason === null && !row.isApproximate && row.benchmarkUnavailableReason === null)).toBe(true);
});


it("acceptance_03 home series uses fixed Nasdaq values and preserves the other paths", () => {
  render(<LocaleProvider routeLocale={null}><HomeSections /></LocaleProvider>);
  const series = JSON.parse(screen.getByTestId("home-performance-preview").getAttribute("data-series") ?? "[]") as PassedSeries[];
  expect(series.map(item => item.key)).toEqual(["portfolio", "sp500", "nasdaq"]);
  expect(HOME_PERFORMANCE_ROWS[0].nasdaq).toBe(0);
  expect(HOME_PERFORMANCE_ROWS.at(-1)?.nasdaq).toBe(0.1557);
  expect(HOME_PERFORMANCE_ROWS.map(row => [row.date, row.portfolio, row.sp500])).toEqual(
    expectedData.HOME_SERIES_POINTS.map(row => row.slice(0, 3)),
  );
});

it.each(LOCALES)("#741 renders the %s macro anchor, two holdings and first CTA in contract order", locale => {
  const { container } = render(<LocaleProvider routeLocale={locale}><HomeSections /></LocaleProvider>);
  const home = catalogs[locale].home;
  const ids = ["top", "audience", "how", "preview", "product-previews", "boundary", "faq"];
  for (let i = 0; i < ids.length - 1; i++) {
    expect(container.querySelector(`#${ids[i]}`)?.compareDocumentPosition(container.querySelector(`#${ids[i + 1]}`)!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  }
  const image = container.querySelector("#how img");
  expect(image).toHaveAttribute("src", `/og/${locale}`);
  expect(image?.compareDocumentPosition(screen.getByRole("heading", { name: home.how.cards[0].title }))).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  expect(container.querySelector("#preview img")).toBeNull();
  expect(within(container.querySelector("#preview")! as HTMLElement).getByText(home.preview.macro.anchor.question)).toBeInTheDocument();
  for (const item of home.preview.analysisItems) expect(screen.getByText(item.heading)).toBeInTheDocument();
  const start = screen.getByRole("heading", { name: home.productPreviews.startCtaHeading });
  const risk = screen.getByRole("heading", { name: home.productPreviews.riskHeading });
  expect(start.compareDocumentPosition(risk)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  expect(within(start.parentElement!).getByRole("link", { name: home.hero.ctaPrimary })).toHaveAttribute("href", "/holdings");
});

it("#741 uses every fixed replay, monthly and risk point without recalculation", () => {
  expect(HOME_MARKET_SHARES).toEqual({ us: "85", uk: "15" });
  expect(HOME_ASSET_CLASS_SHARES).toEqual({ stock: "52", usTechEtf: "17", usBroadEtf: "13", bondFund: "5", cash: "13" });
  expect(HOME_GROUP_SHARES).toEqual({ longTerm: "52", compounding: "35", opportunity: "13" });
  expect(HOME_SERIES_POINTS.map(p => [p.date, p.portfolio, p.sp500, p.nasdaq, p.weights])).toEqual(expectedData.HOME_SERIES_POINTS);
  for (const p of HOME_SERIES_POINTS) expect(p.weights.reduce((s, n) => s + n, 0)).toBeCloseTo(1, 10);
  expect(HOME_MONTHLY_ROWS.map(r => [r.month, r.startDate, r.endDate, r.portfolio, r.benchmark])).toEqual(expectedData.HOME_MONTHLY_ROWS);
  const points = [expectedData.RISK.PORTFOLIO_POINTS, expectedData.RISK.SP500_POINTS, expectedData.RISK.NASDAQ_POINTS];
  const current = ["0.160", "0.110", "0.173"];
  [HOME_RISK_SAMPLE.portfolio_vol, ...HOME_RISK_SAMPLE.benchmark_vols].forEach((series, i) => {
    expect(series).toMatchObject({ status: "ok", current: current[i], tier: "medium", window_start: "2026-06-25", window_end: "2026-09-18", sample_count: 60 });
    expect(series.points.map(p => [p.date, p.vol])).toEqual(points[i]);
  });
  expect(HOME_RISK_SAMPLE.beta).toEqual({ status: "ok", value: "1.28", sample_count: 60 });
  expect(HOME_RISK_SAMPLE.risk).toEqual({ status: "ok", label: "caution" });
  expect(HOME_RISK_SAMPLE.deviation).toEqual({ status: "ok", delta: 1 });
  expect(HOME_RISK_SAMPLE.manual_valuation_share).toBe("0");
});

it("747 stacks heading tags under the three homepage headings below sm", () => {
  const { container } = render(
    <LocaleProvider routeLocale="en">
      <HomeSections />
    </LocaleProvider>,
  );
  for (const id of ["how", "preview", "faq"]) {
    const section = container.querySelector(`#${id}`);
    if (!(section instanceof HTMLElement)) throw new Error(`missing #${id}`);
    const row = section.querySelector("h2")?.parentElement;
    if (!(row instanceof HTMLElement)) throw new Error(`missing heading row #${id}`);
    expect(row, id).toHaveClass("flex-col", "sm:flex-row");
    const tag = row.querySelector("span");
    if (!(tag instanceof HTMLElement)) throw new Error(`missing tag #${id}`);
    expect(tag, id).toHaveClass("sm:whitespace-nowrap");
    expect(tag.classList.contains("whitespace-nowrap"), id).toBe(false);
  }
});
