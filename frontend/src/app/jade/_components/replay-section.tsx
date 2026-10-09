"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { CalculatingOverlay } from "@/components/calculating-overlay";
import { ReplayRangeTabs } from "./replay-range-tabs";
import { useLocale } from "@/app/_components/locale-provider";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CurrencySwitcher } from "@/app/portfolio/_components/currency-switcher";
import type { BaseCurrency } from "@/app/portfolio/_components/currencies";
import { BenchmarkSingleSelectMenu } from "@/app/portfolio/performance/_components/benchmark-single-select-menu";
import { BENCHMARK_COLORS, PORTFOLIO_COLOR } from "@/app/portfolio/performance/_components/performance-colors";
import { formatShortDate, formatTickPct } from "@/app/portfolio/performance/_components/performance-format";
import { getJadeReplay, type BenchmarkCode, type JadeReplay, type ReplayHolding, type ReplayMetrics, type ReplayRange } from "@/lib/api";

const metricKeys = ["cumulative_return", "annualized_return", "annualized_vol", "max_drawdown", "worst_day", "worst_month"] as const;
function pct(value: string | null) { return value === null ? "—" : formatTickPct(Number(value)); }
function timestamp(day: string) { return new Date(`${day}T00:00:00Z`).getTime(); }

export function ReplaySection({ onSettled }: { onSettled?: (currency: BaseCurrency, benchmark: BenchmarkCode) => void }) {
  const settledRef = useRef(onSettled);
  useEffect(() => { settledRef.current = onSettled; }, [onSettled]);
  const t = useTranslations("jade.replay");
  const tCommon = useTranslations("common");
  const portfolio = useTranslations("portfolio");
  const { locale } = useLocale();
  const [data, setData] = useState<JadeReplay | null>(null);
  const [currency, setCurrency] = useState<BaseCurrency>("USD");
  const [benchmark, setBenchmark] = useState<BenchmarkCode>("sp500");
  const [range, setRange] = useState<ReplayRange>("1Y");
  const [pending, setPending] = useState(true);
  const [error, setError] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  useEffect(() => {
    let alive = true;
    getJadeReplay(undefined, undefined, "1Y").then((result) => {
      if (alive) { setData(result); setCurrency(result.base_currency as BaseCurrency); setBenchmark(result.benchmark); setRange(result.range); settledRef.current?.(result.base_currency as BaseCurrency, result.benchmark); }
    }).catch(() => { if (alive) setError(true); }).finally(() => { if (alive) setPending(false); });
    const media = window.matchMedia("(min-width: 640px)");
    const update = () => setDetailsOpen(media.matches);
    update(); media.addEventListener("change", update);
    return () => { alive = false; media.removeEventListener("change", update); };
  }, []);
  async function change(nextCurrency: BaseCurrency, nextBenchmark: BenchmarkCode, nextRange: ReplayRange) {
    if (pending) return;
    setCurrency(nextCurrency); setBenchmark(nextBenchmark); setRange(nextRange); setPending(true); setError(false);
    try {
      const result = await getJadeReplay(nextCurrency, nextBenchmark, nextRange);
      setData(result); setCurrency(result.base_currency as BaseCurrency); setBenchmark(result.benchmark); setRange(result.range); onSettled?.(result.base_currency as BaseCurrency, result.benchmark);
    } catch {
      if (data) { setCurrency(data.base_currency as BaseCurrency); setBenchmark(data.benchmark); setRange(data.range); }
      setError(true);
    } finally { setPending(false); }
  }
  function method(row: ReplayHolding) {
    if (row.method === "excluded") return t(`excluded.${row.excluded_reason ?? "data_unavailable"}`);
    if (row.method === "proxy") return t("methods.proxy", { proxy_name: row.proxy_name ?? "", proxy_symbol: row.proxy_symbol ?? "" });
    if (row.method === "head_proxy") return t("methods.head_proxy", { own_first_date: row.own_first_date ?? "", proxy_name: row.proxy_name ?? "", proxy_symbol: row.proxy_symbol ?? "", beta: row.beta ?? "—" });
    return t(`methods.${row.method}`);
  }
  function metricCell(metrics: ReplayMetrics | null, key: (typeof metricKeys)[number]) {
    return <div className="min-w-0 break-words">
      {pct(metrics?.[key] ?? null)}
      {metrics && key === "max_drawdown" && <p className="text-muted-foreground">{metrics.max_drawdown_peak} → {metrics.max_drawdown_trough}</p>}
      {metrics && key === "worst_day" && <p className="text-muted-foreground">{metrics.worst_day_date}</p>}
      {metrics && key === "worst_month" && metrics.worst_month !== null && <p className="text-muted-foreground">{metrics.worst_month_label}</p>}
    </div>;
  }
  const names = portfolio.raw("performance.benchmarkNames") as Record<BenchmarkCode, string>;
  const benchmarkLabel = data ? `${names[data.benchmark]} (${data.benchmark_symbol})` : names[benchmark];
  const chart = data?.points.map((p) => ({ time: timestamp(p.date), portfolio: p.portfolio === null ? null : Number(p.portfolio), benchmark: p.benchmark === null ? null : Number(p.benchmark) })) ?? [];
  const coverage = data ? (["own", "proxy", "head_proxy", "cash", "cash_assumed"] as const).filter((key) => Number(data.coverage[`${key}_share`]) > 0).map((key) => t(`coverage.${key}`, { pct: (Number(data.coverage[`${key}_share`]) * 100).toFixed(2) })).join(locale === "en" ? ", " : "，") : "";
  return <Card className="min-w-0">
    <CardHeader><CardTitle>{t("title")}</CardTitle></CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-4 px-4 text-sm">
      <p>{t("intro")}</p>
      <CalculatingOverlay active={pending} label={tCommon("calculating")}>
      <div className="flex min-w-0 flex-col gap-4">
      <div data-testid="replay-settings" className="flex min-w-0 flex-wrap gap-3">
        <ReplayRangeTabs value={range} disabled={pending} onChange={(next) => void change(currency, benchmark, next)} />
        <CurrencySwitcher value={currency} disabled={pending} onChange={(next) => void change(next, benchmark, range)} />
        <BenchmarkSingleSelectMenu label={t("benchmarkLabel")} value={benchmark} disabled={pending} onChange={(next) => void change(currency, next, range)} />
      </div>
        <div data-testid="replay-chart" className="h-60 w-full min-w-0" aria-label={t("title")}>
          {data?.status === "ok" && <ResponsiveContainer width="100%" height="100%"><LineChart data={chart} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
            <XAxis type="number" dataKey="time" domain={["dataMin", "dataMax"]} tickFormatter={(v: number) => formatShortDate(new Date(v).toISOString().slice(0, 10), locale)} tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} minTickGap={24} />
            <YAxis tickFormatter={formatTickPct} tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} width={48} />
            <Tooltip labelFormatter={(v) => formatShortDate(new Date(Number(v)).toISOString().slice(0, 10), locale)} formatter={(v) => typeof v === "number" ? formatTickPct(v) : "—"} contentStyle={{ background: "var(--card)", borderColor: "var(--border)" }} />
            {data.holdings.map((h) => {
              const ownFirstDate = h.own_first_date;
              if (h.method !== "head_proxy" || !ownFirstDate) return null;
              return <ReferenceLine key={h.holding_id} x={timestamp(ownFirstDate)} stroke="var(--muted-foreground)" strokeDasharray="3 3" />;
            })}
            <Line dataKey="portfolio" name={t("portfolioLabel")} stroke={PORTFOLIO_COLOR} strokeWidth={2.5} dot={false} isAnimationActive={false} />
            <Line dataKey="benchmark" name={benchmarkLabel} stroke={BENCHMARK_COLORS[data.benchmark]} strokeWidth={1.5} dot={false} isAnimationActive={false} />
          </LineChart></ResponsiveContainer>}
          {data && data.status !== "ok" && <div className="flex h-full items-center justify-center"><p>{t(data.status === "no_holdings" ? "noHoldings" : data.status)}</p></div>}
        </div>
        {data?.status === "ok" && <div className="flex flex-wrap gap-3 text-xs"><span style={{ color: PORTFOLIO_COLOR }}>{t("portfolioLabel")}</span><span style={{ color: BENCHMARK_COLORS[data.benchmark] }}>{benchmarkLabel}</span></div>}
        <div data-testid="replay-metrics" className="grid min-w-0 grid-cols-3 gap-x-2 gap-y-3 text-xs sm:text-sm">
          <span /><span className="min-w-0 break-words">{t("portfolioLabel")}</span><span className="min-w-0 break-words">{t("benchmarkLabel")}</span>
          {metricKeys.map((key) => <div key={key} className="contents"><span className="min-w-0 break-words">{t(`metrics.${key}`)}</span>{metricCell(data?.status === "ok" ? data.metrics.portfolio : null, key)}{metricCell(data?.status === "ok" ? data.metrics.benchmark : null, key)}</div>)}
        </div>
        {data?.status === "ok" && data.metrics.benchmark === null && <p>{t("benchmarkUnavailable")}</p>}
      {data?.status === "ok" && ["1M", "3M", "6M", "YTD"].includes(data.range) && <p>{t("annualizedShortNote")}</p>}
      <details open={detailsOpen} onToggle={(e) => setDetailsOpen(e.currentTarget.open)} className="min-w-0 break-words">
        <summary className="cursor-pointer font-medium">{t("dataAndMethod")}</summary>
        <div className="mt-3 flex min-w-0 flex-col gap-3">
          {data && (data.status === "ok" || data.status === "insufficient") && <>
          <p>{t("window", { start: data.window_start, end: data.window_end, count: data.sample_count })}{data.skipped_days > 0 && <> {t("skippedDays", { count: data.skipped_days })}</>}</p>
          <p>{coverage}</p>
          {data.coverage.data_quality && <p>{t("dataQuality")}</p>}
          {data.coverage.pending_share && <p>{t("pendingShare", { pct: (Number(data.coverage.pending_share) * 100).toFixed(2) })}</p>}
          <ul data-testid="replay-holdings" className="flex min-w-0 flex-col gap-3 break-words">
            {data.holdings.map((h) => <li key={h.holding_id} className="min-w-0"><p className="font-medium">{h.name}{h.weight !== null && <> · {pct(h.weight)}</>}</p><p>{method(h)}</p>
              {h.own_history_unavailable && <p className="text-muted-foreground">{t("ownHistoryUnavailable")}</p>}
              {h.method === "head_proxy" && <><p>{t("volatility", { own_vol: pct(h.own_vol), proxy_segment_vol: pct(h.proxy_segment_vol) })}</p>{h.beta_samples !== null && h.beta_samples < 60 && <p>{t("betaFallback")}</p>}</>}
            </li>)}
          </ul>
          </>}
          <p>{t("sources")}</p><p>{t("method", { currency })}</p><p>{t("limitations")}</p>
        </div>
      </details>
      </div>
      </CalculatingOverlay>
      {error && <p role="alert" className="text-destructive">{t("loadError")}</p>}
    </CardContent>
  </Card>;
}
