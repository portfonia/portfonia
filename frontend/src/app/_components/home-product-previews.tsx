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
import {
  PerformanceChart,
  type ChartSeriesSpec,
} from "@/app/portfolio/performance/_components/performance-chart";
import { type Messages } from "@/locales";

const SAMPLE_CURRENCY = "USD";
const PORTFOLIO_COLOR = "var(--chart-1)";
const SP500_COLOR = "var(--chart-2)";
const CSI300_COLOR = "var(--chart-csi300)";

export const HOME_MARKET_SHARES: Record<string, string> = {
  us: "55",
  hk: "21",
  cn: "15",
  other: "9",
};

export const HOME_ASSET_CLASS_SHARES: Record<string, string> = {
  stock: "48",
  usBroadEtf: "22",
  usTechEtf: "16",
  bondFund: "9",
  other: "5",
};

export const HOME_GROUP_SHARES: Record<string, string> = {
  longTerm: "52",
  retirement: "28",
  opportunity: "20",
};

const HOME_ALLOCATION_CLASSES = [
  "STOCK",
  "EQUITY_US_BROAD",
  "EQUITY_US_TECH",
  "BOND_FUND",
  "CASH_EQUIV",
] as const;

const HOME_SERIES_POINTS: readonly {
  date: string;
  portfolio: number;
  sp500: number;
  csi300: number;
  weights: readonly [number, number, number, number, number];
}[] = [
  { date: "2026-01-01", portfolio: 0, sp500: 0, csi300: 0, weights: [0.52, 0.2, 0.14, 0.09, 0.05] },
  { date: "2026-02-27", portfolio: 0.012, sp500: 0.009, csi300: 0.021, weights: [0.51, 0.2, 0.15, 0.09, 0.05] },
  { date: "2026-03-31", portfolio: -0.006, sp500: 0.004, csi300: -0.018, weights: [0.5, 0.21, 0.15, 0.09, 0.05] },
  { date: "2026-04-30", portfolio: 0.018, sp500: 0.015, csi300: 0.006, weights: [0.5, 0.21, 0.15, 0.09, 0.05] },
  { date: "2026-05-29", portfolio: 0.031, sp500: 0.028, csi300: 0.012, weights: [0.49, 0.21, 0.16, 0.09, 0.05] },
  { date: "2026-06-30", portfolio: 0.044, sp500: 0.036, csi300: 0.019, weights: [0.49, 0.22, 0.15, 0.09, 0.05] },
  { date: "2026-07-31", portfolio: 0.057, sp500: 0.049, csi300: 0.027, weights: [0.48, 0.22, 0.16, 0.09, 0.05] },
  { date: "2026-08-31", portfolio: 0.071, sp500: 0.058, csi300: 0.033, weights: [0.48, 0.22, 0.16, 0.09, 0.05] },
  { date: "2026-09-18", portfolio: 0.0842, sp500: 0.067, csi300: 0.041, weights: [0.48, 0.22, 0.16, 0.09, 0.05] },
];

const HOME_PERFORMANCE_ROWS: ChartSeriesRow[] = HOME_SERIES_POINTS.map((point) => ({
  date: point.date,
  portfolio: point.portfolio,
  sp500: point.sp500,
  csi300: point.csi300,
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

const HOME_MONTHLY_ROWS: MonthlyBarRow[] = [
  { month: "2026-01", startDate: "2026-01-01", endDate: "2026-01-31", portfolio: 0.012, benchmark: 0.009, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-02", startDate: "2026-02-01", endDate: "2026-02-27", portfolio: -0.004, benchmark: 0.003, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-03", startDate: "2026-03-01", endDate: "2026-03-31", portfolio: -0.014, benchmark: -0.005, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-04", startDate: "2026-04-01", endDate: "2026-04-30", portfolio: 0.024, benchmark: 0.011, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-05", startDate: "2026-05-01", endDate: "2026-05-29", portfolio: 0.013, benchmark: 0.013, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-06", startDate: "2026-06-01", endDate: "2026-06-30", portfolio: 0.012, benchmark: 0.008, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-07", startDate: "2026-07-01", endDate: "2026-07-31", portfolio: 0.012, benchmark: 0.012, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-08", startDate: "2026-08-01", endDate: "2026-08-31", portfolio: 0.013, benchmark: 0.009, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-09", startDate: "2026-09-01", endDate: "2026-09-18", portfolio: 0.012, benchmark: 0.008, partialReason: "month_to_date", isApproximate: false, benchmarkUnavailableReason: null },
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
      key: "csi300",
      label: tPerf("benchmarkNames.csi300"),
      color: CSI300_COLOR,
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
