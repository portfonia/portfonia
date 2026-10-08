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
const CSI300_COLOR = BENCHMARK_COLORS.csi300;

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
  { date: "2026-01-01", portfolio: 0.0, sp500: 0.0, csi300: 0.0, weights: [0.52,  0.2,  0.14,  0.09,  0.05] },
  { date: "2026-01-02", portfolio: 0.0028, sp500: 0.0019, csi300: 0.0, weights: [0.52,  0.2,  0.14,  0.09,  0.05] },
  { date: "2026-01-09", portfolio: 0.0244, sp500: 0.0176, csi300: 0.0279, weights: [0.52,  0.2,  0.14,  0.09,  0.05] },
  { date: "2026-01-16", portfolio: 0.0186, sp500: 0.0138, csi300: 0.022, weights: [0.52,  0.2,  0.14,  0.09,  0.05] },
  { date: "2026-01-23", portfolio: 0.0135, sp500: 0.0102, csi300: 0.0157, weights: [0.52,  0.2,  0.14,  0.09,  0.05] },
  { date: "2026-01-30", portfolio: 0.021, sp500: 0.0137, csi300: 0.0165, weights: [0.52,  0.2,  0.14,  0.09,  0.05] },
  { date: "2026-02-06", portfolio: 0.0202, sp500: 0.0127, csi300: 0.003, weights: [0.51,  0.2,  0.15,  0.09,  0.05] },
  { date: "2026-02-13", portfolio: 0.0017, sp500: -0.0014, csi300: 0.0066, weights: [0.51,  0.2,  0.15,  0.09,  0.05] },
  { date: "2026-02-20", portfolio: 0.0167, sp500: 0.0094, csi300: 0.0066, weights: [0.51,  0.2,  0.15,  0.09,  0.05] },
  { date: "2026-02-27", portfolio: 0.0136, sp500: 0.0049, csi300: 0.0174, weights: [0.51,  0.2,  0.15,  0.09,  0.05] },
  { date: "2026-03-06", portfolio: -0.0123, sp500: -0.0154, csi300: 0.0066, weights: [0.5,  0.21,  0.15,  0.09,  0.05] },
  { date: "2026-03-13", portfolio: -0.0294, sp500: -0.0312, csi300: 0.0085, weights: [0.5,  0.21,  0.15,  0.09,  0.05] },
  { date: "2026-03-20", portfolio: -0.0474, sp500: -0.0495, csi300: -0.0136, weights: [0.5,  0.21,  0.15,  0.09,  0.05] },
  { date: "2026-03-27", portfolio: -0.0721, sp500: -0.0696, csi300: -0.0275, weights: [0.5,  0.21,  0.15,  0.09,  0.05] },
  { date: "2026-03-31", portfolio: -0.046, sp500: -0.0463, csi300: -0.0389, weights: [0.5,  0.21,  0.15,  0.09,  0.05] },
  { date: "2026-04-02", portfolio: -0.0334, sp500: -0.0384, csi300: -0.0389, weights: [0.5,  0.21,  0.15,  0.09,  0.05] },
  { date: "2026-04-10", portfolio: 0.006, sp500: -0.0042, csi300: 0.0014, weights: [0.5,  0.21,  0.15,  0.09,  0.05] },
  { date: "2026-04-17", portfolio: 0.0566, sp500: 0.041, csi300: 0.0213, weights: [0.5,  0.21,  0.15,  0.09,  0.05] },
  { date: "2026-04-24", portfolio: 0.0663, sp500: 0.0467, csi300: 0.0301, weights: [0.5,  0.21,  0.15,  0.09,  0.05] },
  { date: "2026-04-30", portfolio: 0.0756, sp500: 0.0531, csi300: 0.0383, weights: [0.5,  0.21,  0.15,  0.09,  0.05] },
  { date: "2026-05-01", portfolio: 0.0766, sp500: 0.0562, csi300: 0.0383, weights: [0.49,  0.21,  0.16,  0.09,  0.05] },
  { date: "2026-05-08", portfolio: 0.1071, sp500: 0.0808, csi300: 0.0523, weights: [0.49,  0.21,  0.16,  0.09,  0.05] },
  { date: "2026-05-15", portfolio: 0.1121, sp500: 0.0822, csi300: 0.0496, weights: [0.49,  0.21,  0.16,  0.09,  0.05] },
  { date: "2026-05-22", portfolio: 0.1213, sp500: 0.0917, csi300: 0.0465, weights: [0.49,  0.21,  0.16,  0.09,  0.05] },
  { date: "2026-05-29", portfolio: 0.1394, sp500: 0.1073, csi300: 0.0566, weights: [0.49,  0.21,  0.16,  0.09,  0.05] },
  { date: "2026-06-05", portfolio: 0.1096, sp500: 0.0786, csi300: 0.0404, weights: [0.49,  0.22,  0.15,  0.09,  0.05] },
  { date: "2026-06-12", portfolio: 0.1172, sp500: 0.0856, csi300: 0.0318, weights: [0.49,  0.22,  0.15,  0.09,  0.05] },
  { date: "2026-06-18", portfolio: 0.1273, sp500: 0.0957, csi300: 0.0673, weights: [0.49,  0.22,  0.15,  0.09,  0.05] },
  { date: "2026-06-26", portfolio: 0.1056, sp500: 0.0743, csi300: 0.0515, weights: [0.49,  0.22,  0.15,  0.09,  0.05] },
  { date: "2026-06-30", portfolio: 0.132, sp500: 0.0955, csi300: 0.0755, weights: [0.49,  0.22,  0.15,  0.09,  0.05] },
  { date: "2026-07-02", portfolio: 0.1265, sp500: 0.0932, csi300: 0.0755, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-07-10", portfolio: 0.1447, sp500: 0.1066, csi300: 0.0326, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-07-17", portfolio: 0.1273, sp500: 0.0894, csi300: -0.0218, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-07-24", portfolio: 0.117, sp500: 0.0828, csi300: 0.0042, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-07-31", portfolio: 0.1307, sp500: 0.0941, csi300: -0.009, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-08-07", portfolio: 0.1813, sp500: 0.1332, csi300: 0.0139, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-08-14", portfolio: 0.1851, sp500: 0.1374, csi300: 0.0078, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-08-21", portfolio: 0.164, sp500: 0.1211, csi300: -0.0024, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-08-28", portfolio: 0.1744, sp500: 0.1265, csi300: -0.0045, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-08-31", portfolio: 0.1709, sp500: 0.1228, csi300: -0.001, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-09-04", portfolio: 0.1739, sp500: 0.1275, csi300: -0.0177, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-09-11", portfolio: 0.1658, sp500: 0.1185, csi300: -0.0259, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-09-18", portfolio: 0.1674, sp500: 0.1176, csi300: -0.0265, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-09-25", portfolio: 0.1811, sp500: 0.1312, csi300: -0.0412, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
  { date: "2026-09-30", portfolio: 0.1658, sp500: 0.1177, csi300: -0.0588, weights: [0.48,  0.22,  0.16,  0.09,  0.05] },
];

export const HOME_PERFORMANCE_ROWS: ChartSeriesRow[] = HOME_SERIES_POINTS.map((point) => ({
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

export const HOME_MONTHLY_ROWS: MonthlyBarRow[] = [
  { month: "2026-01", startDate: "2026-01-01", endDate: "2026-01-30", portfolio: 0.021, benchmark: 0.0137, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-02", startDate: "2026-02-01", endDate: "2026-02-27", portfolio: -0.0072, benchmark: -0.0087, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-03", startDate: "2026-03-01", endDate: "2026-03-31", portfolio: -0.0588, benchmark: -0.051, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-04", startDate: "2026-04-01", endDate: "2026-04-30", portfolio: 0.1275, benchmark: 0.1042, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-05", startDate: "2026-05-01", endDate: "2026-05-29", portfolio: 0.0593, benchmark: 0.0515, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-06", startDate: "2026-06-01", endDate: "2026-06-30", portfolio: -0.0065, benchmark: -0.0107, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-07", startDate: "2026-07-01", endDate: "2026-07-31", portfolio: -0.0011, benchmark: -0.0013, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-08", startDate: "2026-08-01", endDate: "2026-08-31", portfolio: 0.0356, benchmark: 0.0262, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
  { month: "2026-09", startDate: "2026-09-01", endDate: "2026-09-30", portfolio: -0.0044, benchmark: -0.0045, partialReason: null, isApproximate: false, benchmarkUnavailableReason: null },
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
