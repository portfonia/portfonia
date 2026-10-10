"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { CalculatingOverlay } from "@/components/calculating-overlay";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useLocale } from "@/app/_components/locale-provider";
import type { BaseCurrency } from "@/app/portfolio/_components/currencies";
import { formatMoney } from "@/app/portfolio/_components/portfolio-helpers";
import { formatShortDate, formatTickPct } from "@/app/portfolio/performance/_components/performance-format";
import { BENCHMARK_COLORS, PORTFOLIO_COLOR } from "@/app/portfolio/performance/_components/performance-colors";
import { getJadeStress, type BenchmarkCode, type JadeStress, type ScenarioId, type StressHolding } from "@/lib/api";

type Settings = { currency: BaseCurrency; benchmark: BenchmarkCode };
const ids: ScenarioId[] = ["dotcom_2000", "gfc_2008", "covid_2020", "rates_2022", "tariffs_2025"];
function matches(settings: Settings, data: JadeStress | null) {
  return data?.base_currency === settings.currency && data?.benchmark === settings.benchmark;
}
function pct(value: string | null | undefined) { return value == null ? "—" : formatTickPct(Number(value)); }
function timestamp(day: string) { return new Date(`${day}T00:00:00Z`).getTime(); }

export function StressSection({ settings }: { settings: Settings | null }) {
  const t = useTranslations("jade.stress");
  const replay = useTranslations("jade.replay");
  const common = useTranslations("common");
  const portfolio = useTranslations("portfolio");
  const { locale } = useLocale();
  const [data, setData] = useState<JadeStress | null>(null);
  const [pending, setPending] = useState(true);
  const [error, setError] = useState(false);
  const [scenario, setScenario] = useState<ScenarioId | null>(null);
  const latestSettings = useRef<Settings | null>(null);
  const savedData = useRef<JadeStress | null>(null);
  const busy = useRef(false);
  const request = useRef<(settings: Settings | null) => void>(() => {});
  useEffect(() => {
    let alive = true;
    function load(next: Settings | null) {
      busy.current = true;
      setPending(true); setError(false);
      const promise = next ? getJadeStress(next.currency, next.benchmark) : getJadeStress();
      promise.then(result => {
        if (alive) { savedData.current = result; setData(result); }
      }).catch(() => { if (alive) setError(true); }).finally(() => {
        busy.current = false;
        if (!alive) return;
        setPending(false);
        const latest = latestSettings.current;
        if (latest && latest !== next && !matches(latest, savedData.current)) load(latest);
      });
    }
    request.current = load;
    load(null);
    return () => { alive = false; };
  }, []);
  const currency = settings?.currency;
  const benchmark = settings?.benchmark;
  useEffect(() => {
    if (!currency || !benchmark) return;
    const next = { currency, benchmark };
    latestSettings.current = next;
    if (!busy.current && !matches(next, savedData.current)) request.current(next);
  }, [currency, benchmark]);
  const selected = data?.scenarios.find(s => s.id === scenario) ?? data?.scenarios.at(-1);
  const selectedId = selected?.id ?? ids.at(-1) ?? "tariffs_2025";
  const ok = selected?.status === "ok";
  const names = portfolio.raw("performance.benchmarkNames") as Record<BenchmarkCode, string>;
  const benchmarkLabel = data ? `${names[data.benchmark]} (${selected?.benchmark_symbol ?? data.benchmark_symbol})` : names[benchmark ?? "sp500"];
  const coverage = selected ? (["own", "proxy", "head_proxy", "cash", "cash_assumed"] as const)
    .filter(key => Number(selected.coverage[`${key}_share`]) > 0)
    .map(key => replay(`coverage.${key}`, { pct: (Number(selected.coverage[`${key}_share`]) * 100).toFixed(2) }))
    .join(locale === "en" ? ", " : "，") : "";
  const chart = selected?.points.map(p => ({ time: timestamp(p.date), portfolio: Number(p.portfolio), benchmark: p.benchmark === null ? null : Number(p.benchmark) })) ?? [];
  const maxContribution = Math.max(...(selected?.contributions.map(c => Math.abs(Number(c.contribution))) ?? []));
  function method(row: StressHolding) {
    if (row.method === "excluded") return replay(`excluded.${row.excluded_reason ?? "data_unavailable"}`);
    if (row.method === "proxy") return replay("methods.proxy", { proxy_name: row.proxy_name ?? "", proxy_symbol: row.proxy_symbol ?? "" });
    if (row.method === "head_proxy") return row.own_first_date === null
      ? t("laterListing", { proxy: row.proxy_symbol ?? "", beta: row.beta ?? "—", samples: row.beta_samples ?? 0 })
      : replay("methods.head_proxy", { own_first_date: row.own_first_date, proxy_name: row.proxy_name ?? "", proxy_symbol: row.proxy_symbol ?? "", beta: row.beta ?? "—" });
    return replay(`methods.${row.method}`);
  }
  return <Card className="min-w-0">
    <CardHeader><CardTitle>{t("title")}</CardTitle></CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-4 px-4 text-sm">
      <p>{t("intro")}</p>
      <CalculatingOverlay active={pending} label={common("calculating")}>
        <div className="flex min-w-0 flex-col gap-4">
          <div role="radiogroup" aria-label={t("scenarioLabel")} className="flex flex-wrap items-center gap-1">
            {(data?.scenarios.map(s => s.id) ?? ids).map(id => <Button key={id} type="button" size="sm" disabled={pending || !data} aria-pressed={id === selectedId} variant={id === selectedId ? "default" : "outline"} onClick={() => setScenario(id)}>{t(`scenarios.${id}.name`)}</Button>)}
          </div>
          <p>{t(`scenarios.${selectedId}.description`)}</p>
          {selected && <p>{t("windowText", { start: selected.window_start, end: selected.window_end, peak: selected.peak_date, trough: selected.trough_date })}</p>}
          {selected && (ok || selected.status === "insufficient") && (selected.coverage.data_quality || selected.substitutions.length > 0 || selected.price_index_symbols.length > 0) && <div
            data-testid="stress-approx-notice" role="note"
            className="rounded-lg border border-amber-300/60 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200 flex min-w-0 flex-col gap-1 break-words"
          >
            <p>{t("approxShare", { pct: (Number(selected.coverage.approx_share_at_start) * 100).toFixed(2) })}</p>
            {selected.coverage.data_quality && <p>{replay("dataQuality")}</p>}
            {selected.substitutions.length > 0 && <p>{t("substitutes", { list: selected.substitutions.map(item => t("substituteItem", { primary: item.primary, symbol: item.symbol, name: item.name })).join(locale === "en" ? ", " : "，") })}</p>}
            {selected.price_index_symbols.length > 0 && <p>{t("priceIndex", { symbols: selected.price_index_symbols.join(", ") })}</p>}
          </div>}
          <div data-testid="stress-table" className="grid min-w-0 grid-cols-3 gap-x-2 gap-y-3 text-xs sm:text-sm">
            <span className="min-w-0 break-words" /><span className="min-w-0 break-words">{t("columns.portfolio")}</span><span className="min-w-0 break-words">{t("columns.benchmark", { benchmark: benchmarkLabel })}</span>
            <div className="contents"><span className="min-w-0 break-words">{t("rows.shock")}</span>
              <div className="min-w-0 break-words">{pct(ok ? selected.shock_return : null)}{ok && selected.shock_amount !== null && <p className="text-muted-foreground">{formatMoney(selected.shock_amount, data?.base_currency ?? "USD")}</p>}</div>
              <div className="min-w-0 break-words">{pct(ok ? selected.benchmark_shock_return : null)}</div>
            </div>
            <div className="contents"><span className="min-w-0 break-words">{t("rows.maxDrawdown")}</span>
              <div className="min-w-0 break-words">{pct(ok ? selected.max_drawdown : null)}{ok && selected.max_drawdown_peak && selected.max_drawdown_trough && <p className="text-muted-foreground">{t("drawdownDates", { peak: selected.max_drawdown_peak, trough: selected.max_drawdown_trough })}</p>}</div>
              <div className="min-w-0 break-words">{pct(ok ? selected.benchmark_max_drawdown : null)}</div>
            </div>
          </div>
          <div data-testid="stress-chart" className="h-60 w-full min-w-0">
            {ok && data && <ResponsiveContainer width="100%" height="100%"><LineChart data={chart} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
              <XAxis type="number" dataKey="time" domain={["dataMin", "dataMax"]} tickFormatter={(v: number) => formatShortDate(new Date(v).toISOString().slice(0, 10), locale)} tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} minTickGap={24} />
              <YAxis tickFormatter={formatTickPct} tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} width={48} />
              <Tooltip labelFormatter={v => formatShortDate(new Date(Number(v)).toISOString().slice(0, 10), locale)} formatter={v => typeof v === "number" ? formatTickPct(v) : "—"} contentStyle={{ background: "var(--card)", borderColor: "var(--border)" }} />
              <ReferenceLine x={timestamp(selected.peak_date)} stroke="var(--muted-foreground)" strokeDasharray="6 3" />
              <ReferenceLine x={timestamp(selected.trough_date)} stroke="var(--muted-foreground)" strokeDasharray="2 3" />
              <Line dataKey="portfolio" name={t("legend.portfolio")} stroke={PORTFOLIO_COLOR} strokeWidth={2.5} dot={false} isAnimationActive={false} />
              {selected.benchmark_status === "ok" && <Line dataKey="benchmark" name={benchmarkLabel} stroke={BENCHMARK_COLORS[data.benchmark]} strokeWidth={1.5} dot={false} isAnimationActive={false} />}
            </LineChart></ResponsiveContainer>}
            {selected && !ok && <div className="flex h-full items-center justify-center"><p>{replay(selected.status === "no_holdings" ? "noHoldings" : selected.status)}</p></div>}
          </div>
          {ok && data && <><div data-testid="stress-legend" className="flex flex-wrap gap-3 text-xs">
            <span style={{ color: PORTFOLIO_COLOR }}>{t("legend.portfolio")}</span>
            {selected.benchmark_status === "ok" && <span style={{ color: BENCHMARK_COLORS[data.benchmark] }}>{benchmarkLabel}</span>}
            <span>{t("legend.peak", { date: selected.peak_date })}</span><span>{t("legend.trough", { date: selected.trough_date })}</span>
          </div><p>{t("chartNote")}</p></>}
          {ok && <div data-testid="stress-contributions" className="flex min-w-0 flex-col gap-3">
            <h3 className="font-medium">{t("contributionsTitle")}</h3>
            {selected.contributions.map(c => { const value = Number(c.contribution); return <div key={c.asset_class} data-contribution={c.asset_class} className="min-w-0">
              <div className="flex flex-wrap justify-between gap-2"><span>{portfolio(`assetClasses.${c.asset_class}`)}</span><span>{pct(c.contribution)}</span></div>
              <div data-track className="relative h-2 w-full min-w-0 bg-muted"><span className="absolute inset-y-0 left-1/2 w-px bg-muted-foreground" /><span data-bar className="absolute inset-y-0" style={{ ...(value < 0 ? { right: "50%" } : { left: "50%" }), width: `${maxContribution ? Math.abs(value) / maxContribution * 50 : 0}%`, background: value < 0 ? "var(--destructive)" : PORTFOLIO_COLOR }} /></div>
            </div>; })}<p>{t("contributionsNote")}</p>
          </div>}
          {selected && (ok || selected.status === "insufficient") && selected.proxy_understates && <p>{t("proxyUnderstates")}</p>}
          <details className="min-w-0 break-words"><summary className="cursor-pointer font-medium">{t("model")}</summary>
            <div className="mt-3 flex min-w-0 flex-col gap-3">
              {selected && (ok || selected.status === "insufficient") && <><p>{t("sampleText", { count: selected.sample_count, first: selected.first_valid_date ?? "—", end: selected.window_end })}</p><p>{coverage}</p></>}
              {selected && <ul className="flex min-w-0 flex-col gap-3 break-words">{selected.holdings.map(h => <li key={h.holding_id} className="min-w-0"><p className="font-medium">{h.name}{h.weight !== null && <> · {pct(h.weight)}</>}</p><p>{method(h)}</p>{h.proxy_for && <p>{t("substituteFor", { symbol: h.proxy_symbol ?? "", primary: h.proxy_for })}</p>}{h.price_only && <p>{t("priceIndexTag")}</p>}</li>)}</ul>}
              {selected && selected.substitutions.length > 0 && <p>{t("substituteMethod")}</p>}
              {selected?.fx_source === "fred" && <p>{t("fredFxNote")}</p>}
              <p>{t("methodText", { currency: data?.base_currency ?? currency ?? "USD" })}</p><p>{t("limitations")}</p>
            </div>
          </details>
        </div>
      </CalculatingOverlay>
      {error && <p role="alert" className="text-destructive">{t("loadError")}</p>}
    </CardContent>
  </Card>;
}
