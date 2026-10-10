"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { CalculatingOverlay } from "@/components/calculating-overlay";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useLocale } from "@/app/_components/locale-provider";
import type { BaseCurrency } from "@/app/portfolio/_components/currencies";
import { formatShortDate, formatTickPct } from "@/app/portfolio/performance/_components/performance-format";
import { PORTFOLIO_COLOR } from "@/app/portfolio/performance/_components/performance-colors";
import { getJadeStyle, type BenchmarkCode, type JadeStyle } from "@/lib/api";

type Settings = { currency: BaseCurrency; benchmark: BenchmarkCode };
const symbols = ["IWF", "IWD", "IWM", "EFA", "EEM", "2800.HK", "510300.SS", "AGG", "TLT", "GLD", "DBC", "VNQ", "BIL"];
function matches(settings: Settings, data: JadeStyle | null) {
  return data?.base_currency === settings.currency && data?.benchmark === settings.benchmark;
}
function pct(value: string | null | undefined) { return value == null ? "—" : formatTickPct(Number(value)); }
function rSquared(value: string | null | undefined) { return value == null ? "—" : Number(value).toFixed(2); }
export function StyleSection({ settings }: { settings: Settings | null }) {
  const t = useTranslations("jade.style");
  const replay = useTranslations("jade.replay");
  const common = useTranslations("common");
  const portfolio = useTranslations("portfolio");
  const { locale } = useLocale();
  const [data, setData] = useState<JadeStyle | null>(null);
  const [pending, setPending] = useState(true);
  const [error, setError] = useState(false);
  const latestSettings = useRef<Settings | null>(null);
  const savedData = useRef<JadeStyle | null>(null);
  const busy = useRef(false);
  const request = useRef<(settings: Settings | null) => void>(() => {});
  useEffect(() => {
    let alive = true;
    function load(next: Settings | null) {
      busy.current = true;
      setPending(true); setError(false);
      const promise = next ? getJadeStyle(next.currency, next.benchmark) : getJadeStyle();
      promise.then(result => {
        if (alive) { savedData.current = result; setData(result); }
      }).catch(() => { if (alive) setError(true); }).finally(() => {
        busy.current = false;
        if (!alive) return;
        setPending(false);
        const latest = latestSettings.current;
        // Only a settings event during this flight can queue another request.
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


  const ok = data?.status === "ok";
  const fitted = ok ? data.portfolio : null;
  const benchmarkFit = ok && data.benchmark_status === "ok" ? data.benchmark_fit : null;
  const names = portfolio.raw("performance.benchmarkNames") as Record<BenchmarkCode, string>;
  const benchmarkLabel = data ? `${names[data.benchmark]} (${data.benchmark_symbol})` : names[benchmark ?? "sp500"];
  const rows = symbols.map((symbol, index) => ({ symbol, index,
    weight: Number(fitted?.weights.find(w => w.symbol === symbol)?.weight ?? 0),
    benchmarkWeight: benchmarkFit?.weights.find(w => w.symbol === symbol)?.weight,
  })).filter(row => row.weight > 0 || Number(row.benchmarkWeight ?? 0) > 0)
    .sort((a,b) => b.weight-a.weight || a.index-b.index);
  const chart = data?.points.map(p => ({ time: new Date(`${p.date}T00:00:00Z`).getTime(), portfolio: Number(p.portfolio), style_mix: Number(p.style_mix) })) ?? [];
  return <Card className="min-w-0">
    <CardHeader><CardTitle>{t("title")}</CardTitle></CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-4 px-4 text-sm">
      <p>{t("intro")}</p>
      <CalculatingOverlay active={pending} label={common("calculating")}>
        <div className="flex min-w-0 flex-col gap-4">
          <div data-testid="style-table" className="grid min-w-0 grid-cols-3 gap-x-2 gap-y-3 text-xs sm:text-sm">
            <span className="min-w-0 break-words">{t("columns.style")}</span><span className="min-w-0 break-words">{t("columns.portfolio")}</span><span className="min-w-0 break-words">{benchmarkLabel}</span>
            {rows.map(row => <div key={row.symbol} data-style-row={row.symbol} className="contents">
              <span className="min-w-0 break-words">{t(`labels.${row.symbol.replaceAll(".","_")}`)} ({row.symbol})</span>
              <div className="min-w-0 break-words">{formatTickPct(row.weight)}<div className="h-2 w-full min-w-0 bg-muted"><span data-style-bar className="block h-full" style={{ width: `${row.weight*100}%`, background: PORTFOLIO_COLOR }} /></div></div>
              <span data-benchmark-cell className="min-w-0 break-words">{pct(row.benchmarkWeight)}</span>
            </div>)}
            <div className="contents"><span className="min-w-0 break-words">{t("rows.rSquared")}</span><span className="min-w-0 break-words">{rSquared(fitted?.r_squared)}</span><span data-benchmark-cell className="min-w-0 break-words">{rSquared(benchmarkFit?.r_squared)}</span></div>
            <div className="contents"><span className="min-w-0 break-words">{t("rows.residualVol")}</span><span className="min-w-0 break-words">{pct(fitted?.residual_vol)}</span><span data-benchmark-cell className="min-w-0 break-words">{pct(benchmarkFit?.residual_vol)}</span></div>
          </div>
          {fitted?.low_fit && <div data-testid="style-low-fit" role="note" className="rounded-lg border border-amber-300/60 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200 break-words">{t("lowFit", { r2: rSquared(fitted.r_squared) })}</div>}
          <div data-testid="style-chart" className="h-60 w-full min-w-0">
            {ok && <ResponsiveContainer width="100%" height="100%"><LineChart data={chart} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
              <XAxis type="number" dataKey="time" domain={["dataMin", "dataMax"]} tickFormatter={(v: number) => formatShortDate(new Date(v).toISOString().slice(0,10), locale)} tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} minTickGap={24} />
              <YAxis tickFormatter={formatTickPct} tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} width={48} />
              <Tooltip labelFormatter={v => formatShortDate(new Date(Number(v)).toISOString().slice(0,10),locale)} formatter={v => typeof v === "number" ? formatTickPct(v) : "—"} contentStyle={{ background: "var(--card)", borderColor: "var(--border)" }} />
              <Line dataKey="portfolio" name={t("legend.portfolio")} stroke={PORTFOLIO_COLOR} strokeWidth={2.5} dot={false} isAnimationActive={false} />
              <Line dataKey="style_mix" name={t("legend.styleMix")} stroke="var(--muted-foreground)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
            </LineChart></ResponsiveContainer>}
          </div>
          {ok && <><div className="flex flex-wrap gap-3 text-xs"><span style={{ color: PORTFOLIO_COLOR }}>{t("legend.portfolio")}</span><span className="text-muted-foreground">{t("legend.styleMix")}</span></div><p>{t("sampleNote",{count:data.sample_count})}</p></>}
          {data && (ok || data.status === "insufficient") && <>{data.coverage.data_quality && <p>{replay("dataQuality")}</p>}{data.proxy_inflates_fit && <p>{t("proxyFit")}</p>}</>}
          {data && !ok && <p>{data.status === "insufficient" ? t("insufficient",{count:data.sample_count,min:data.min_samples}) : replay(data.status === "no_holdings" ? "noHoldings" : data.status)}</p>}
          <details className="min-w-0 break-words"><summary className="cursor-pointer font-medium">{t("model")}</summary><div className="mt-3 flex min-w-0 flex-col gap-3">
            {data && <p>{t("windowText", {start:data.window_start,end:data.window_end,count:data.sample_count})}</p>}
            <p>{t("methodText",{currency:data?.base_currency ?? currency ?? "USD"})}</p><p>{t("limitations")}</p>
          </div></details>
        </div>
      </CalculatingOverlay>
      {error && <p role="alert" className="text-destructive">{t("loadError")}</p>}
    </CardContent>
  </Card>;
}
