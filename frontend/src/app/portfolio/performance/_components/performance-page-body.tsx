"use client";

// Portfolio Performance page body (issue #360 Phase 2, UI defaults #382).
// Owns the fetch state for GET /portfolio/performance
// (range/twr/benchmarks/filters/currency) and renders: range tabs, benchmark
// + dataset-dimension multi-selects, the TWR mode toggle, the header metric
// card, and the chart card with its empty/short-history/approximate-data
// handling. First visit: range 1M, benchmarks [sp500].
//
// Wording rules enforced here (design doc §1/§2 D5/D7):
// - The header dollar figure is always labeled "market value change", never
//   return dollars (twr=true or not).
// - The percentage is a since-tracking performance measure (approx TWR when
//   the toggle is on), a different concept from the $ figure — the
//   disclosure line under the metrics says so explicitly.
// - Portfolio history is only ever drawn from real daily snapshots; the
//   empty/short copy never implies a reconstructed full investment history.

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { useLocale } from "@/app/_components/locale-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  BENCHMARK_CODES,
  DEFAULT_BENCHMARKS,
  DEFAULT_MONTHLY_BENCHMARK,
  DEFAULT_PERFORMANCE_RANGE,
  type BenchmarkCode,
  type PerformanceRange,
  type PortfolioPerformanceQuery,
  type PortfolioPerformanceResponse,
  type PortfolioSummary,
  getPortfolioPerformance,
} from "@/lib/api";
import { formatMoney, pnlColorClass } from "../../_components/portfolio-helpers";
import { CurrencySwitcher } from "../../_components/currency-switcher";
import { DEFAULT_BASE_CURRENCY, type BaseCurrency } from "../../_components/currencies";
import { AllocationChart } from "./allocation-chart";
import { ASSET_CLASS_COLORS, DEFAULT_ASSET_CLASS_COLOR, buildAllocationData } from "./allocation-data";
import { BenchmarkSingleSelectMenu } from "./benchmark-single-select-menu";
import { buildMonthlyRows } from "./monthly-data";
import { MonthlyPerformanceChart } from "./monthly-performance-chart";
import { MultiSelectMenu } from "./multi-select-menu";
import { PerformanceChart, type ChartSeriesSpec } from "./performance-chart";
import {
  PORTFOLIO_APPROX_KEY,
  PORTFOLIO_KEY,
  buildChartData,
  hasApproximateSegment,
  seriesHasSingleValue,
} from "./performance-data";
import { formatFullDate, formatSignedPct, toRatio } from "./performance-format";
import { RangeTabs } from "./range-tabs";

const PORTFOLIO_COLOR = "var(--chart-1)";
// csi300 uses a dedicated Performance-page token (issue #433), not the
// shared `--chart-5` the /portfolio breakdown charts still use — `--chart-5`
// is a very dark near-black neutral in the dark theme, effectively
// invisible against the card background.
const BENCHMARK_COLORS: Record<BenchmarkCode, string> = {
  sp500: "var(--chart-2)",
  dow30: "var(--chart-3)",
  nasdaq: "var(--chart-4)",
  csi300: "var(--chart-csi300)",
};

const DIMENSION_META = [
  { id: "markets", labelKey: "filterMarketLabel", field: "market" },
  { id: "groups", labelKey: "filterGroupLabel", field: "portfolio" },
  { id: "brokers", labelKey: "filterBrokerLabel", field: "broker" },
  { id: "accounts", labelKey: "filterAccountLabel", field: "account" },
] as const satisfies readonly {
  id: keyof DatasetFilters;
  labelKey: "filterMarketLabel" | "filterGroupLabel" | "filterBrokerLabel" | "filterAccountLabel";
  field: "market" | "portfolio" | "broker" | "account";
}[];

interface DatasetFilters {
  markets: string[];
  groups: string[];
  brokers: string[];
  accounts: string[];
}

const EMPTY_FILTERS: DatasetFilters = { markets: [], groups: [], brokers: [], accounts: [] };

function filtersActive(filters: DatasetFilters): boolean {
  return (
    filters.markets.length > 0 ||
    filters.groups.length > 0 ||
    filters.brokers.length > 0 ||
    filters.accounts.length > 0
  );
}

// Filter options come from the CURRENT book's own snapshot-time labels
// (market/portfolio/broker/account as recorded on each holding). Deliberately
// NOT the summary's by_* fallback display keys ("Ungrouped"/"Other"): those
// are presentation literals for null labels, while the backend's Filters
// compares raw stored values — passing "Ungrouped" would filter to nothing.
// A dimension value that only exists on long-sold lots is not listed (it
// still shows in the unfiltered history); the empty dimension = all case
// covers it.
function dimensionOptions(
  summary: PortfolioSummary | null,
  field: "market" | "portfolio" | "broker" | "account",
): string[] {
  if (!summary) return [];
  const values = new Set<string>();
  for (const holding of summary.holdings) {
    const raw = holding[field];
    const value = raw?.trim();
    if (value) values.add(value);
  }
  return [...values].sort((a, b) => a.localeCompare(b));
}

export function PerformancePageBody({
  initialSummary,
  initialLoadError,
}: {
  initialSummary: PortfolioSummary | null;
  initialLoadError: boolean;
}) {
  const t = useTranslations("portfolio");
  const { locale } = useLocale();

  // Issue #350 item 1: seed from the user's own persisted report-currency
  // preference (what the server-rendered summary resolved), same as the
  // /portfolio overview page.
  const [currency, setCurrency] = useState<BaseCurrency>(
    (initialSummary?.base_currency as BaseCurrency | undefined) ?? DEFAULT_BASE_CURRENCY,
  );
  const [range, setRange] = useState<PerformanceRange>(DEFAULT_PERFORMANCE_RANGE);
  const [twr, setTwr] = useState(true);
  const [benchmarks, setBenchmarks] = useState<BenchmarkCode[]>([...DEFAULT_BENCHMARKS]);
  // Issue #433: independent single-select, unrelated to the cumulative
  // chart's `benchmarks` multi-select above.
  const [monthlyBenchmark, setMonthlyBenchmark] = useState<BenchmarkCode>(DEFAULT_MONTHLY_BENCHMARK);
  const [filters, setFilters] = useState<DatasetFilters>(EMPTY_FILTERS);
  const [response, setResponse] = useState<PortfolioPerformanceResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const requestSeq = useRef(0);

  // All params come from a single current-state snapshot so a control
  // handler can refetch with exactly one changed value.
  const paramsFor = (
    overrides: Partial<PortfolioPerformanceQuery>,
  ): PortfolioPerformanceQuery => ({
    range,
    twr,
    benchmarks,
    markets: filters.markets,
    groups: filters.groups,
    brokers: filters.brokers,
    accounts: filters.accounts,
    baseCurrency: currency,
    monthlyBenchmark,
    ...overrides,
  });

  // Fire one query and drop stale responses (a slower earlier response must
  // not overwrite a newer selection — same single-flight reasoning as the
  // overview page's async startTransition comment, PR #322). A 401 routes
  // through logout() inside the api helper (issue #235). Called from event
  // handlers only, never from an effect body (react-hooks/set-state-in-
  // effect).
  const performQuery = (params: PortfolioPerformanceQuery) => {
    const seq = ++requestSeq.current;
    setIsLoading(true);
    setLoadError(false);
    getPortfolioPerformance(params)
      .then((data) => {
        if (seq !== requestSeq.current) return;
        setResponse(data);
      })
      .catch(() => {
        if (seq !== requestSeq.current) return;
        setLoadError(true);
      })
      .finally(() => {
        if (seq === requestSeq.current) setIsLoading(false);
      });
  };

  // Initial load on mount (isLoading already starts true, so no synchronous
  // setState is needed here — only the promise callbacks touch state).
  useEffect(() => {
    let cancelled = false;
    const seq = ++requestSeq.current;
    getPortfolioPerformance(paramsFor({}))
      .then((data) => {
        if (cancelled || seq !== requestSeq.current) return;
        setResponse(data);
      })
      .catch(() => {
        if (cancelled || seq !== requestSeq.current) return;
        setLoadError(true);
      })
      .finally(() => {
        if (!cancelled && seq === requestSeq.current) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // Mount-only: control changes refetch through the handlers below, not
    // this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const chartData = useMemo(
    () => buildChartData(response?.portfolio ?? null, response?.benchmarks ?? []),
    [response],
  );
  const allocationData = useMemo(
    () => buildAllocationData(response?.allocation ?? null),
    [response],
  );
  const monthlyRows = useMemo(
    () => buildMonthlyRows(response?.monthly_performance ?? null),
    [response],
  );
  const hasApprox = hasApproximateSegment(response?.portfolio ?? null);
  const portfolioEmpty = !response || response.portfolio.empty;
  const qualityFlags = response?.portfolio.quality_flags ?? [];
  const filterActive = filtersActive(filters);

  const chartSeries: ChartSeriesSpec[] = useMemo(() => {
    const names = t.raw("performance.benchmarkNames");
    const series: ChartSeriesSpec[] = [];
    if (response && !response.portfolio.empty) {
      series.push({
        key: PORTFOLIO_KEY,
        label: t("performance.chartPortfolioLabel"),
        color: PORTFOLIO_COLOR,
        isPortfolio: true,
        singletonDot: seriesHasSingleValue(chartData.rows, PORTFOLIO_KEY),
      });
      if (hasApprox) {
        series.push({
          key: PORTFOLIO_APPROX_KEY,
          label: t("performance.chartPortfolioLabel"),
          color: PORTFOLIO_COLOR,
          dashed: true,
          isPortfolio: true,
          singletonDot: seriesHasSingleValue(chartData.rows, PORTFOLIO_APPROX_KEY),
        });
      }
    }
    for (const benchmark of chartData.drawnBenchmarks) {
      const ownBaseline =
        benchmark.normalization === "own_start" && benchmark.anchor_date
          ? t("performance.comparisonOwnBaseline", {
              date: formatFullDate(benchmark.anchor_date, locale),
            })
          : null;
      series.push({
        key: benchmark.index_code,
        label: ownBaseline
          ? `${names[benchmark.index_code]} — ${ownBaseline}`
          : names[benchmark.index_code],
        color: BENCHMARK_COLORS[benchmark.index_code],
        singletonDot: seriesHasSingleValue(chartData.rows, benchmark.index_code),
      });
    }
    return series;
  }, [response, chartData, hasApprox, t, locale]);

  // Legend mirrors the drawn lines, collapsing the portfolio's solid+dashed
  // pair back into one entry (the dashed hint line explains the second
  // stroke style).
  const legendSeries = chartSeries.filter((spec) => spec.key !== PORTFOLIO_APPROX_KEY);

  const benchmarkNames = t.raw("performance.benchmarkNames");
  const assetClassNames = t.raw("assetClasses") as Record<string, string>;
  const unavailableBenchmarks =
    response?.benchmarks.filter((benchmark) => !benchmark.displayable) ?? [];
  const comparisonNotices =
    response?.benchmarks.filter(
      (benchmark) =>
        benchmark.displayable &&
        (benchmark.comparison_status === "baseline_only" ||
          benchmark.comparison_status === "anchor_unavailable" ||
          benchmark.comparison_status === "incomplete_window" ||
          benchmark.normalization === "own_start"),
    ) ?? [];
  const sharedAnchor =
    response?.benchmarks.find((benchmark) => benchmark.normalization === "portfolio_start")
      ?.anchor_date ??
    response?.portfolio.start_date ??
    null;

  const changeRange = (next: PerformanceRange) => {
    setRange(next);
    performQuery(paramsFor({ range: next }));
  };
  const changeTwr = (next: boolean) => {
    setTwr(next);
    performQuery(paramsFor({ twr: next }));
  };
  const changeBenchmarks = (next: BenchmarkCode[]) => {
    setBenchmarks(next);
    performQuery(paramsFor({ benchmarks: next }));
  };
  const changeMonthlyBenchmark = (next: BenchmarkCode) => {
    setMonthlyBenchmark(next);
    performQuery(paramsFor({ monthlyBenchmark: next }));
  };
  const changeCurrency = (next: BaseCurrency) => {
    setCurrency(next);
    performQuery(paramsFor({ baseCurrency: next }));
  };
  const setDimension = (id: keyof DatasetFilters, next: string[]) => {
    const merged = { ...filters, [id]: next };
    setFilters(merged);
    performQuery(
      paramsFor({
        markets: merged.markets,
        groups: merged.groups,
        brokers: merged.brokers,
        accounts: merged.accounts,
      }),
    );
  };
  const resetFilters = () => {
    setFilters(EMPTY_FILTERS);
    performQuery(paramsFor({ markets: [], groups: [], brokers: [], accounts: [] }));
  };

  const currencyLabel = response?.meta.base_currency ?? currency;
  const metricPct = response ? toRatio(response.header.value_change_pct) : null;

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="font-heading text-2xl font-medium">{t("performance.pageTitle")}</h1>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            {t("performance.pageSubtitle")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <CurrencySwitcher value={currency} onChange={changeCurrency} disabled={isLoading} />
          <Link
            href="/portfolio"
            className="text-sm text-muted-foreground underline underline-offset-4 hover:text-foreground"
          >
            {t("performance.overviewLink")}
          </Link>
        </div>
      </header>

      {initialLoadError && (
        <p role="alert" className="text-sm text-destructive">
          {t("performance.summaryLoadError")}
        </p>
      )}

      <Card>
        <CardContent className="flex flex-col gap-4 px-4">
          <RangeTabs value={range} onChange={changeRange} disabled={isLoading} />

          <div className="flex flex-wrap items-center gap-2">
            <MultiSelectMenu
              label={t("performance.benchmarkLabel")}
              options={BENCHMARK_CODES.map((code) => ({
                value: code,
                label: benchmarkNames[code],
              }))}
              selected={benchmarks}
              onChange={(next) => changeBenchmarks(next as BenchmarkCode[])}
              // Benchmarks are NOT an omit-param filter: absent/empty
              // means zero series (portfolio-only, issue #382), so "All"
              // must mean every code selected. Clearing the last chip is
              // allowed; do not expand [] back to the full catalog.
              allMode="all-options"
            />
            {DIMENSION_META.map(({ id, labelKey, field }) => (
              <MultiSelectMenu
                key={id}
                label={t(`performance.${labelKey}`)}
                options={dimensionOptions(initialSummary, field).map((value) => ({
                  value,
                  label: value,
                }))}
                selected={filters[id]}
                onChange={(next) => setDimension(id, next)}
                disabled={!initialSummary || initialSummary.holdings.length === 0}
              />
            ))}

            <div
              role="radiogroup"
              aria-label={t("performance.twrLabel")}
              className="flex items-center gap-1"
            >
              <Button
                type="button"
                variant={twr ? "default" : "outline"}
                size="sm"
                aria-pressed={twr}
                disabled={isLoading}
                onClick={() => changeTwr(true)}
              >
                {t("performance.twrOn")}
              </Button>
              <Button
                type="button"
                variant={twr ? "outline" : "default"}
                size="sm"
                aria-pressed={!twr}
                disabled={isLoading}
                onClick={() => changeTwr(false)}
              >
                {t("performance.twrOff")}
              </Button>
            </div>

            {filterActive && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={isLoading}
                onClick={resetFilters}
              >
                {t("performance.resetFilters")}
              </Button>
            )}
          </div>

          {loadError && response && (
            <p role="alert" className="text-sm text-destructive">
              {t("performance.refreshError")}
            </p>
          )}
        </CardContent>
      </Card>

      {!portfolioEmpty && response ? (
        <Card>
          <CardHeader>
            <CardTitle>{t("performance.metricsTitle")}</CardTitle>
            <CardDescription>{t("performance.metricsScope")}</CardDescription>
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-3 px-4 sm:grid-cols-3">
            <div className="flex flex-col gap-1">
              <span className="text-xs text-foreground/60">
                {t("performance.metricValueLabel")}
              </span>
              <span className="font-heading text-2xl tabular-nums">
                {formatMoney(response.header.value_base, currencyLabel)}
              </span>
            </div>
            <div className="flex flex-col gap-1">
              <span className="text-xs text-foreground/60">
                {t("performance.metricChangeLabel")}
              </span>
              <span className={`tabular-nums ${pnlColorClass(response.header.value_change_base)}`}>
                {formatMoney(response.header.value_change_base, currencyLabel)}
              </span>
            </div>
            <div className="flex flex-col gap-1">
              <span className="text-xs text-foreground/60">
                {twr
                  ? t("performance.metricTwrPctLabel", {
                      date: formatFullDate(response.portfolio.start_date ?? "", locale),
                    })
                  : t("performance.metricRawPctLabel")}
              </span>
              <span className={`tabular-nums ${pnlColorClass(response.header.value_change_pct)}`}>
                {formatSignedPct(metricPct)}
              </span>
            </div>
          </CardContent>
          <CardContent className="px-4 pt-0">
            <p className="text-xs text-muted-foreground">
              {twr
                ? t("performance.metricDisclosureTwr")
                : t("performance.metricDisclosureRaw")}
            </p>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-3">
          <div>
            <CardTitle>{t("performance.chartTitle")}</CardTitle>
            <CardDescription>
              {portfolioEmpty
                ? t("performance.chartDescriptionBenchmarksOnly")
                : t("performance.chartDescriptionTracked")}
              {!portfolioEmpty && response?.portfolio.tracking_start ? (
                <span>
                  {" "}
                  {t("performance.trackingSinceNote", {
                    date: formatFullDate(response.portfolio.tracking_start, locale),
                  })}
                </span>
              ) : null}
              {qualityFlags.length > 0 ? (
                <span className="mt-1 block">
                  {qualityFlags
                    .map((flag) => t(`performance.qualityFlag.${flag}`))
                    .join(" ")}
                </span>
              ) : null}
            </CardDescription>
          </div>
          {(qualityFlags.length > 0 || hasApprox) && (
            <Badge variant="secondary" data-testid="approx-badge">
              {t("performance.approxBadgeLabel")}
            </Badge>
          )}
        </CardHeader>
        <CardContent className="flex flex-col gap-3 px-4">
          {loadError && !response && (
            <div className="flex flex-col items-start gap-2">
              <p role="alert" className="text-sm text-destructive">
                {t("performance.loadError")}
              </p>
              <Button type="button" variant="outline" size="sm" onClick={() => performQuery(paramsFor({}))}>
                {t("performance.retry")}
              </Button>
            </div>
          )}

          {isLoading && (
            <p role="status" className="text-xs text-muted-foreground">
              {t("performance.loading")}
            </p>
          )}

          {portfolioEmpty && response && (
            <div
              role="status"
              data-testid="portfolio-empty-state"
              className="rounded-lg border border-dashed border-border px-4 py-3"
            >
              <p className="text-sm font-medium">
                {filterActive
                  ? t("performance.filteredEmptyTitle")
                  : t("performance.emptyTitle")}
              </p>
              <p className="mt-1 text-sm text-muted-foreground">
                {filterActive
                  ? t("performance.filteredEmptyBody")
                  : t("performance.emptyBody")}
              </p>
            </div>
          )}

          {chartData.rows.length > 0 && (
            <>
              <PerformanceChart
                rows={chartData.rows}
                series={chartSeries}
                pointMeta={chartData.pointMeta}
                anchorDate={!portfolioEmpty ? sharedAnchor : null}
              />
              <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm" data-testid="chart-legend">
                {legendSeries.map((spec) => (
                  <li key={spec.key} className="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      className="h-0.5 w-4 rounded-full"
                      style={{ backgroundColor: spec.color }}
                    />
                    {spec.label}
                  </li>
                ))}
                {hasApprox && (
                  <li className="flex items-center gap-2 text-muted-foreground">
                    <span
                      aria-hidden="true"
                      className="h-0.5 w-4"
                      style={{
                        backgroundImage:
                          "repeating-linear-gradient(90deg, var(--chart-1) 0 4px, transparent 4px 7px)",
                      }}
                    />
                    {t("performance.approxDashLegend")}
                  </li>
                )}
              </ul>
              {!portfolioEmpty && sharedAnchor ? (
                <p className="text-xs text-muted-foreground">
                  {t("performance.indexHistoryRelativeTo", {
                    date: formatFullDate(sharedAnchor, locale),
                  })}
                </p>
              ) : null}
              {comparisonNotices.map((benchmark) => {
                const name = benchmarkNames[benchmark.index_code];
                if (benchmark.comparison_status === "baseline_only") {
                  return (
                    <p key={benchmark.index_code} className="text-xs text-muted-foreground">
                      {t("performance.comparisonBaselineOnly", { name })}
                    </p>
                  );
                }
                if (benchmark.comparison_status === "anchor_unavailable") {
                  return (
                    <p key={benchmark.index_code} className="text-xs text-muted-foreground">
                      {t("performance.comparisonAnchorUnavailable", { name })}
                    </p>
                  );
                }
                if (benchmark.comparison_status === "incomplete_window") {
                  return (
                    <p key={benchmark.index_code} className="text-xs text-muted-foreground">
                      {t("performance.comparisonIncompleteWindow", { name })}
                    </p>
                  );
                }
                if (benchmark.normalization === "own_start" && benchmark.anchor_date) {
                  return (
                    <p key={benchmark.index_code} className="text-xs text-muted-foreground">
                      {t("performance.comparisonOwnBaseline", {
                        date: formatFullDate(benchmark.anchor_date, locale),
                      })}
                    </p>
                  );
                }
                return null;
              })}
              {unavailableBenchmarks.map((benchmark) => (
                <p key={benchmark.index_code} className="text-xs text-muted-foreground">
                  {t("performance.benchmarkNoUsableHistory", {
                    name: benchmarkNames[benchmark.index_code],
                  })}
                </p>
              ))}
            </>
          )}

          {chartData.rows.length === 0 && !isLoading && !loadError && (
            <p data-testid="chart-no-data" className="text-sm text-muted-foreground">
              {t("performance.chartNoData")}
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("performance.allocationTitle")}</CardTitle>
          <CardDescription>{t("performance.allocationDescription")}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 px-4">
          {allocationData.rows.length > 0 ? (
            <>
              <AllocationChart
                rows={allocationData.rows}
                assetClasses={allocationData.assetClasses}
                assetClassNames={assetClassNames}
                pointMeta={allocationData.pointMeta}
              />
              <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
                {allocationData.assetClasses.map((cls) => (
                  <li key={cls} className="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      className="size-2.5 shrink-0 rounded-full"
                      style={{ backgroundColor: ASSET_CLASS_COLORS[cls] ?? DEFAULT_ASSET_CLASS_COLOR }}
                    />
                    {assetClassNames[cls] ?? cls}
                  </li>
                ))}
              </ul>
              {allocationData.hasIncomplete && (
                <p role="status" className="text-xs text-muted-foreground">
                  {t("performance.allocationIncompleteBanner")}
                </p>
              )}
            </>
          ) : (
            <p data-testid="allocation-no-data" className="text-sm text-muted-foreground">
              {t("performance.chartNoData")}
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-3">
          <div>
            <CardTitle>{t("performance.monthlyTitle")}</CardTitle>
            <CardDescription>{t("performance.monthlyDescription")}</CardDescription>
          </div>
          <BenchmarkSingleSelectMenu
            label={t("performance.monthlyBenchmarkLabel")}
            value={monthlyBenchmark}
            onChange={changeMonthlyBenchmark}
            disabled={isLoading}
          />
        </CardHeader>
        <CardContent className="flex flex-col gap-3 px-4">
          {monthlyRows.length > 0 ? (
            <MonthlyPerformanceChart
              rows={monthlyRows}
              benchmarkName={benchmarkNames[monthlyBenchmark]}
              benchmarkColor={BENCHMARK_COLORS[monthlyBenchmark]}
            />
          ) : (
            <p data-testid="monthly-no-data" className="text-sm text-muted-foreground">
              {t("performance.chartNoData")}
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
