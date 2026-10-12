"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

import { BreakdownChart } from "@/app/portfolio/_components/breakdown-chart";
import {
  ASSET_CLASS_COLORS,
  DEFAULT_ASSET_CLASS_COLOR,
  type AllocationPointMeta,
  type AllocationRow,
} from "@/app/portfolio/performance/_components/allocation-data";
import { AllocationChart } from "@/app/portfolio/performance/_components/allocation-chart";
import { type MonthlyBarRow } from "@/app/portfolio/performance/_components/monthly-data";
import { MonthlyPerformanceChart } from "@/app/portfolio/performance/_components/monthly-performance-chart";
import { type ChartSeriesRow } from "@/app/portfolio/performance/_components/performance-data";
import { BENCHMARK_COLORS, PORTFOLIO_COLOR } from "@/app/portfolio/performance/_components/performance-colors";
import {
  PerformanceChart,
  type ChartSeriesSpec,
} from "@/app/portfolio/performance/_components/performance-chart";
import { type Messages } from "@/locales";
import { HomeRiskPreview } from "./home-risk-preview";

const SAMPLE_CURRENCY = "USD";
const SP500_COLOR = BENCHMARK_COLORS.sp500;
const NASDAQ_COLOR = BENCHMARK_COLORS.nasdaq;

export const HOME_MARKET_SHARES: Record<string, string> = { us: "85", uk: "15" };

export const HOME_ASSET_CLASS_SHARES: Record<string, string> = { stock: "52", usTechEtf: "17", usBroadEtf: "13", bondFund: "5", cash: "13" };

export const HOME_GROUP_SHARES: Record<string, string> = { longTerm: "52", compounding: "35", opportunity: "13" };

const HOME_ALLOCATION_CLASSES = [
  "STOCK",
  "EQUITY_US_BROAD",
  "EQUITY_US_TECH",
  "BOND_FUND",
  "CASH_EQUIV",
] as const;

export const HOME_SERIES_POINTS: readonly {
  date: string;
  portfolio: number;
  sp500: number;
  nasdaq: number;
  weights: readonly [number, number, number, number, number];
}[] = [
  { date: "2026-01-01", portfolio: 0.0, sp500: 0.0, nasdaq: 0.0, weights: [0.53, 0.13, 0.16, 0.05, 0.13] },
  { date: "2026-01-02", portfolio: 0.0064, sp500: 0.0019, nasdaq: -0.0003, weights: [0.53, 0.13, 0.16, 0.05, 0.13] },
  { date: "2026-01-09", portfolio: 0.0114, sp500: 0.0176, nasdaq: 0.0185, weights: [0.53, 0.13, 0.16, 0.05, 0.13] },
  { date: "2026-01-16", portfolio: 0.0148, sp500: 0.0138, nasdaq: 0.0118, weights: [0.53, 0.13, 0.16, 0.05, 0.13] },
  { date: "2026-01-23", portfolio: 0.009, sp500: 0.0102, nasdaq: 0.0112, weights: [0.53, 0.13, 0.16, 0.05, 0.13] },
  { date: "2026-01-30", portfolio: 0.0071, sp500: 0.0137, nasdaq: 0.0095, weights: [0.53, 0.13, 0.16, 0.05, 0.13] },
  { date: "2026-02-06", portfolio: -0.001, sp500: 0.0127, nasdaq: -0.0091, weights: [0.53, 0.13, 0.16, 0.05, 0.13] },
  { date: "2026-02-13", portfolio: -0.0011, sp500: -0.0014, nasdaq: -0.0299, weights: [0.53, 0.13, 0.15, 0.05, 0.14] },
  { date: "2026-02-20", portfolio: 0.0046, sp500: 0.0094, nasdaq: -0.0153, weights: [0.53, 0.13, 0.15, 0.05, 0.14] },
  { date: "2026-02-27", portfolio: 0.0004, sp500: 0.0049, nasdaq: -0.0247, weights: [0.53, 0.13, 0.15, 0.05, 0.14] },
  { date: "2026-03-06", portfolio: -0.0405, sp500: -0.0154, nasdaq: -0.0368, weights: [0.52, 0.13, 0.16, 0.05, 0.14] },
  { date: "2026-03-13", portfolio: -0.0381, sp500: -0.0312, nasdaq: -0.0489, weights: [0.52, 0.13, 0.16, 0.05, 0.14] },
  { date: "2026-03-20", portfolio: -0.0613, sp500: -0.0495, nasdaq: -0.0686, weights: [0.51, 0.13, 0.16, 0.06, 0.14] },
  { date: "2026-03-27", portfolio: -0.0824, sp500: -0.0696, nasdaq: -0.0987, weights: [0.51, 0.13, 0.15, 0.06, 0.15] },
  { date: "2026-03-31", portfolio: -0.0641, sp500: -0.0463, nasdaq: -0.0711, weights: [0.51, 0.13, 0.16, 0.06, 0.14] },
  { date: "2026-04-02", portfolio: -0.0489, sp500: -0.0384, nasdaq: -0.0586, weights: [0.52, 0.13, 0.16, 0.05, 0.14] },
  { date: "2026-04-10", portfolio: -0.0117, sp500: -0.0042, nasdaq: -0.0146, weights: [0.52, 0.13, 0.16, 0.05, 0.14] },
  { date: "2026-04-17", portfolio: 0.0305, sp500: 0.041, nasdaq: 0.0528, weights: [0.53, 0.13, 0.16, 0.05, 0.13] },
  { date: "2026-04-24", portfolio: 0.0455, sp500: 0.0467, nasdaq: 0.0686, weights: [0.53, 0.13, 0.16, 0.05, 0.13] },
  { date: "2026-04-30", portfolio: 0.0439, sp500: 0.0531, nasdaq: 0.071, weights: [0.53, 0.13, 0.16, 0.05, 0.13] },
  { date: "2026-05-01", portfolio: 0.052, sp500: 0.0562, nasdaq: 0.0806, weights: [0.53, 0.13, 0.16, 0.05, 0.13] },
  { date: "2026-05-08", portfolio: 0.0753, sp500: 0.0808, nasdaq: 0.1293, weights: [0.53, 0.13, 0.17, 0.05, 0.12] },
  { date: "2026-05-15", portfolio: 0.0653, sp500: 0.0822, nasdaq: 0.1284, weights: [0.52, 0.13, 0.17, 0.05, 0.13] },
  { date: "2026-05-22", portfolio: 0.0681, sp500: 0.0917, nasdaq: 0.1335, weights: [0.52, 0.13, 0.17, 0.05, 0.13] },
  { date: "2026-05-29", portfolio: 0.0897, sp500: 0.1073, nasdaq: 0.1605, weights: [0.53, 0.13, 0.17, 0.05, 0.12] },
  { date: "2026-06-05", portfolio: 0.0616, sp500: 0.0786, nasdaq: 0.1062, weights: [0.52, 0.13, 0.17, 0.05, 0.13] },
  { date: "2026-06-12", portfolio: 0.0579, sp500: 0.0856, nasdaq: 0.1139, weights: [0.52, 0.13, 0.17, 0.05, 0.13] },
  { date: "2026-06-18", portfolio: 0.0864, sp500: 0.0957, nasdaq: 0.1409, weights: [0.53, 0.13, 0.17, 0.05, 0.12] },
  { date: "2026-06-26", portfolio: 0.0442, sp500: 0.0743, nasdaq: 0.0884, weights: [0.52, 0.13, 0.17, 0.05, 0.13] },
  { date: "2026-06-30", portfolio: 0.0843, sp500: 0.0955, nasdaq: 0.1279, weights: [0.53, 0.13, 0.17, 0.05, 0.12] },
  { date: "2026-07-02", portfolio: 0.0595, sp500: 0.0932, nasdaq: 0.1115, weights: [0.52, 0.13, 0.17, 0.05, 0.13] },
  { date: "2026-07-10", portfolio: 0.0693, sp500: 0.1066, nasdaq: 0.1308, weights: [0.52, 0.13, 0.17, 0.05, 0.13] },
  { date: "2026-07-17", portfolio: 0.0363, sp500: 0.0894, nasdaq: 0.098, weights: [0.51, 0.14, 0.17, 0.05, 0.13] },
  { date: "2026-07-24", portfolio: 0.0215, sp500: 0.0828, nasdaq: 0.0746, weights: [0.51, 0.14, 0.17, 0.05, 0.13] },
  { date: "2026-07-31", portfolio: 0.0534, sp500: 0.0941, nasdaq: 0.0917, weights: [0.52, 0.13, 0.17, 0.05, 0.13] },
  { date: "2026-08-07", portfolio: 0.0868, sp500: 0.1332, nasdaq: 0.1484, weights: [0.52, 0.14, 0.17, 0.05, 0.12] },
  { date: "2026-08-14", portfolio: 0.0919, sp500: 0.1374, nasdaq: 0.15, weights: [0.52, 0.14, 0.17, 0.05, 0.12] },
  { date: "2026-08-21", portfolio: 0.0799, sp500: 0.1211, nasdaq: 0.1264, weights: [0.52, 0.14, 0.17, 0.05, 0.12] },
  { date: "2026-08-28", portfolio: 0.0867, sp500: 0.1265, nasdaq: 0.136, weights: [0.53, 0.13, 0.17, 0.05, 0.12] },
  { date: "2026-08-31", portfolio: 0.0817, sp500: 0.1228, nasdaq: 0.1346, weights: [0.52, 0.14, 0.17, 0.05, 0.12] },
  { date: "2026-09-04", portfolio: 0.0871, sp500: 0.1275, nasdaq: 0.1405, weights: [0.53, 0.13, 0.17, 0.05, 0.12] },
  { date: "2026-09-11", portfolio: 0.0823, sp500: 0.1185, nasdaq: 0.133, weights: [0.53, 0.13, 0.17, 0.05, 0.12] },
  { date: "2026-09-18", portfolio: 0.0802, sp500: 0.1176, nasdaq: 0.1411, weights: [0.52, 0.14, 0.17, 0.05, 0.12] },
  { date: "2026-09-25", portfolio: 0.101, sp500: 0.1312, nasdaq: 0.1646, weights: [0.53, 0.13, 0.17, 0.05, 0.12] },
  { date: "2026-09-30", portfolio: 0.0984, sp500: 0.1177, nasdaq: 0.1557, weights: [0.53, 0.13, 0.17, 0.05, 0.12] },
];

export const HOME_PERFORMANCE_ROWS: ChartSeriesRow[] = HOME_SERIES_POINTS.map((point) => ({
  date: point.date,
  portfolio: point.portfolio,
  sp500: point.sp500,
  nasdaq: point.nasdaq,
}));

const HOME_ALLOCATION_ROWS: AllocationRow[] = HOME_SERIES_POINTS.map((point) => {
  const row: AllocationRow = { date: point.date };
  HOME_ALLOCATION_CLASSES.forEach((assetClass, index) => {
    row[assetClass] = point.weights[index];
  });
  return row;
});

const HOME_ALLOCATION_POINT_META: Record<string, AllocationPointMeta> = Object.fromEntries(
  HOME_SERIES_POINTS.map((point) => [
    point.date,
    { isIncomplete: false, excludedHoldingCount: 0, isGap: false },
  ]),
);

export const HOME_MONTHLY_ROWS: MonthlyBarRow[] = [
  { month: "2026-01", startDate: "2026-01-01", endDate: "2026-01-30", portfolio: 0.0071, benchmark: 0.0137, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-02", startDate: "2026-02-01", endDate: "2026-02-27", portfolio: -0.0066, benchmark: -0.0087, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-03", startDate: "2026-03-01", endDate: "2026-03-31", portfolio: -0.0644, benchmark: -0.0509, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-04", startDate: "2026-04-01", endDate: "2026-04-30", portfolio: 0.1154, benchmark: 0.1042, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-05", startDate: "2026-05-01", endDate: "2026-05-29", portfolio: 0.0439, benchmark: 0.0515, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-06", startDate: "2026-06-01", endDate: "2026-06-30", portfolio: -0.005, benchmark: -0.0106, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-07", startDate: "2026-07-01", endDate: "2026-07-31", portfolio: -0.0284, benchmark: -0.0013, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-08", startDate: "2026-08-01", endDate: "2026-08-31", portfolio: 0.0268, benchmark: 0.0262, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-09", startDate: "2026-09-01", endDate: "2026-09-30", portfolio: 0.0155, benchmark: -0.0045, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
];

function percentLabel(_key: string, value: number): string {
  return `${value}%`;
}

export function HomeProductPreviews() {
  const t = useTranslations("home");
  const copy = t.raw("productPreviews") as Messages["home"]["productPreviews"];
  const tPortfolio = useTranslations("portfolio");
  const assetClassCatalog = tPortfolio.raw("assetClasses") as Messages["portfolio"]["assetClasses"];
  const tPerf = useTranslations("portfolio.performance");

  const series: ChartSeriesSpec[] = [
    {
      key: "portfolio",
      label: tPerf("chartPortfolioLabel"),
      color: PORTFOLIO_COLOR,
      isPortfolio: true,
      connectNulls: true,
    },
    {
      key: "sp500",
      label: tPerf("benchmarkNames.sp500"),
      color: SP500_COLOR,
    },
    {
      key: "nasdaq",
      label: tPerf("benchmarkNames.nasdaq"),
      color: NASDAQ_COLOR,
    },
  ];
  const passedSeries = series.map((item) => ({
    key: item.key,
    label: item.label,
    color: item.color,
    isPortfolio: item.isPortfolio === true,
    connectNulls: item.connectNulls === true,
  }));

  const assetClassNames: Record<string, string> = Object.fromEntries(
    HOME_ALLOCATION_CLASSES.map((assetClass) => [assetClass, assetClassCatalog[assetClass]]),
  );

  return (
    <section id="product-previews" className="px-6 py-16 sm:py-20">
      <div className="mx-auto flex max-w-4xl flex-col gap-16">
        <div className="rounded-2xl border border-white/10 bg-card p-7 sm:p-11">
          <h2 className="max-w-[18ch] font-serif text-2xl sm:text-3xl">{copy.startCtaHeading}</h2>
          <p className="mt-4 max-w-xl text-base leading-relaxed text-foreground/70">{copy.startCtaBody}</p>
          <Link
            href="/holdings"
            className="mt-8 inline-flex rounded-md bg-primary px-6 py-3 text-sm font-semibold text-primary-foreground"
          >
            {t("hero.ctaPrimary")}
          </Link>
        </div>
        <div>
          <div className="mb-8 border-b border-white/10 pb-5">
            <h2 className="font-serif text-2xl sm:text-3xl">{copy.riskHeading}</h2>
          </div>
          <p className="mb-6 max-w-2xl text-base leading-relaxed text-foreground/70">{copy.riskBody}</p>
          <div className="min-w-0 space-y-6 rounded-xl border border-white/10 bg-card p-4" data-testid="home-risk-preview">
            <HomeRiskPreview />
          </div>
        </div>

        <div className="rounded-2xl border border-white/10 bg-card p-7 sm:p-11">
          <h2 className="max-w-[18ch] font-serif text-2xl sm:text-3xl">{copy.riskCtaHeading}</h2>
          <p className="mt-4 max-w-xl text-base leading-relaxed text-foreground/70">{copy.riskCtaBody}</p>
          <Link
            href="/holdings"
            className="mt-8 inline-flex rounded-md bg-primary px-6 py-3 text-sm font-semibold text-primary-foreground"
          >
            {t("hero.ctaPrimary")}
          </Link>
        </div>

        <div>
          <div className="mb-8 border-b border-white/10 pb-5">
            <h2 className="font-serif text-2xl sm:text-3xl">{copy.portfolioHeading}</h2>
          </div>
          <p className="mb-6 max-w-2xl text-base leading-relaxed text-foreground/70">{copy.portfolioBody}</p>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <BreakdownChart
              title={copy.chartMarket}
              data={HOME_MARKET_SHARES}
              currency={SAMPLE_CURRENCY}
              emptyLabel={tPortfolio("chartEmpty")}
              labelFor={(key) => copy.markets[key as keyof typeof copy.markets]}
              formatValue={percentLabel}
              showShareOfTotal={false}
            />
            <BreakdownChart
              title={copy.chartAssetClass}
              data={HOME_ASSET_CLASS_SHARES}
              currency={SAMPLE_CURRENCY}
              emptyLabel={tPortfolio("chartEmpty")}
              labelFor={(key) => copy.assetClasses[key as keyof typeof copy.assetClasses]}
              formatValue={percentLabel}
              showShareOfTotal={false}
            />
            <BreakdownChart
              title={copy.chartGroup}
              data={HOME_GROUP_SHARES}
              currency={SAMPLE_CURRENCY}
              emptyLabel={tPortfolio("chartEmpty")}
              labelFor={(key) => copy.groups[key as keyof typeof copy.groups]}
              formatValue={percentLabel}
              showShareOfTotal={false}
            />
          </div>
        </div>

        <div>
          <div className="mb-8 border-b border-white/10 pb-5">
            <h2 className="font-serif text-2xl sm:text-3xl">{copy.performanceHeading}</h2>
          </div>
          <p className="mb-6 max-w-2xl text-base leading-relaxed text-foreground/70">{copy.performanceBody}</p>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_16rem]">
            <div
              className="min-w-0 rounded-xl border border-white/10 bg-card p-4"
              data-testid="home-performance-preview"
              data-series={JSON.stringify(passedSeries)}
              data-points={JSON.stringify(
                HOME_PERFORMANCE_ROWS.map((row) => ({ date: row.date, portfolio: row.portfolio })),
              )}
            >
              <PerformanceChart
                rows={HOME_PERFORMANCE_ROWS}
                series={series}
                pointMeta={{}}
                anchorDate={null}
              />
              <ul className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
                {series.map((spec) => (
                  <li key={spec.key} className="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      className="h-0.5 w-4 rounded-full"
                      style={{ backgroundColor: spec.color }}
                    />
                    {spec.label}
                  </li>
                ))}
              </ul>
            </div>
            <div className="rounded-xl border border-white/10 bg-card p-6">
              <p className="text-sm text-foreground/60">{copy.metricLabel}</p>
              <p className="mt-2 font-serif text-4xl tabular-nums">{copy.metricValue}</p>
              <p className="mt-2 font-mono text-xs uppercase tracking-wide text-foreground/45">
                {copy.yearToDate}
              </p>
            </div>
          </div>
          <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div
              className="min-w-0 rounded-xl border border-white/10 bg-card p-4"
              data-testid="home-allocation-preview"
              data-asset-classes={HOME_ALLOCATION_CLASSES.join(",")}
              data-asset-class-names={JSON.stringify(assetClassNames)}
              data-dates={HOME_ALLOCATION_ROWS.map((row) => row.date).join(",")}
            >
              <h3 className="mb-3 font-serif text-base">{tPerf("allocationTitle")}</h3>
              <AllocationChart
                rows={HOME_ALLOCATION_ROWS}
                assetClasses={HOME_ALLOCATION_CLASSES}
                assetClassNames={assetClassNames}
                pointMeta={HOME_ALLOCATION_POINT_META}
              />
              <ul className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
                {HOME_ALLOCATION_CLASSES.map((assetClass) => (
                  <li key={assetClass} className="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      className="size-2.5 shrink-0 rounded-full"
                      style={{
                        backgroundColor: ASSET_CLASS_COLORS[assetClass] ?? DEFAULT_ASSET_CLASS_COLOR,
                      }}
                    />
                    {assetClassNames[assetClass]}
                  </li>
                ))}
              </ul>
            </div>
            <div
              className="min-w-0 rounded-xl border border-white/10 bg-card p-4"
              data-testid="home-monthly-preview"
              data-benchmark-name={tPerf("benchmarkNames.sp500")}
              data-benchmark-color={SP500_COLOR}
              data-rows={JSON.stringify(
                HOME_MONTHLY_ROWS.map((row) => ({ benchmark: row.benchmark })),
              )}
            >
              <h3 className="mb-3 font-serif text-base">{tPerf("monthlyTitle")}</h3>
              <MonthlyPerformanceChart
                rows={HOME_MONTHLY_ROWS}
                benchmarkName={tPerf("benchmarkNames.sp500")}
                benchmarkColor={SP500_COLOR}
              />
              <ul className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
                <li className="flex items-center gap-2">
                  <span
                    aria-hidden="true"
                    className="size-2.5 shrink-0 rounded-full"
                    style={{ backgroundColor: PORTFOLIO_COLOR }}
                  />
                  {tPerf("monthlyPortfolioLabel")}
                </li>
                <li className="flex items-center gap-2">
                  <span
                    aria-hidden="true"
                    className="size-2.5 shrink-0 rounded-full"
                    style={{ backgroundColor: SP500_COLOR }}
                  />
                  {tPerf("benchmarkNames.sp500")}
                </li>
              </ul>
            </div>
          </div>
        </div>

        <div className="rounded-2xl border border-white/10 bg-card p-7 sm:p-11">
          <h2 className="max-w-[18ch] font-serif text-2xl sm:text-3xl">{copy.ctaHeading}</h2>
          <p className="mt-4 max-w-xl text-base leading-relaxed text-foreground/70">{copy.ctaBody}</p>
          <Link
            href="/holdings"
            className="mt-8 inline-flex rounded-md bg-primary px-6 py-3 text-sm font-semibold text-primary-foreground"
          >
            {t("hero.ctaPrimary")}
          </Link>
        </div>
      </div>
    </section>
  );
}
