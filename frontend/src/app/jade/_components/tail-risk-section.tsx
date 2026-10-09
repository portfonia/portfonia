"use client";

import type { BaseCurrency } from "@/app/portfolio/_components/currencies";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { Bar, BarChart, ReferenceLine, ResponsiveContainer, XAxis, YAxis } from "recharts";
import { CalculatingOverlay } from "@/components/calculating-overlay";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useLocale } from "@/app/_components/locale-provider";
import { formatMoney } from "@/app/portfolio/_components/portfolio-helpers";
import { formatTickPct } from "@/app/portfolio/performance/_components/performance-format";
import { PORTFOLIO_COLOR } from "@/app/portfolio/performance/_components/performance-colors";
import { getJadeTailRisk, type BenchmarkCode, type JadeTailRisk, type TailCell } from "@/lib/api";

type Settings = { currency: BaseCurrency; benchmark: BenchmarkCode };
function matches(settings: Settings, data: JadeTailRisk | null) {
  return data?.base_currency === settings.currency && data?.benchmark === settings.benchmark;
}
function loss(value: string | null | undefined) {
  return value == null ? "—" : formatTickPct(-Number(value));
}

export function TailRiskSection({ settings }: { settings: Settings | null }) {
  const t = useTranslations("jade.tailRisk");
  const replay = useTranslations("jade.replay");
  const common = useTranslations("common");
  const portfolio = useTranslations("portfolio");
  const { locale } = useLocale();
  const [data, setData] = useState<JadeTailRisk | null>(null);
  const [pending, setPending] = useState(true);
  const [error, setError] = useState(false);
  const [level, setLevel] = useState<95 | 99>(95);
  const latestSettings = useRef<Settings | null>(null);
  const savedData = useRef<JadeTailRisk | null>(null);
  const busy = useRef(false);
  const request = useRef<(settings: Settings | null) => void>(() => {});
  useEffect(() => {
    let alive = true;
    function load(next: Settings | null) {
      busy.current = true;
      setPending(true); setError(false);
      const promise = next ? getJadeTailRisk(next.currency, next.benchmark) : getJadeTailRisk();
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
  const selected = ok ? data.levels.find(item => item.level === level) : undefined;
  const names = portfolio.raw("performance.benchmarkNames") as Record<BenchmarkCode, string>;
  const benchmarkLabel = data ? `${names[data.benchmark]} (${data.benchmark_symbol})` : names[benchmark ?? "sp500"];
  const coverage = data ? (["own", "proxy", "head_proxy", "cash", "cash_assumed"] as const)
    .filter(key => Number(data.coverage[`${key}_share`]) > 0)
    .map(key => replay(`coverage.${key}`, { pct: (Number(data.coverage[`${key}_share`]) * 100).toFixed(2) }))
    .join(locale === "en" ? ", " : "，") : "";
  function valueCell(cell: TailCell | null | undefined, metric: "var" | "cvar", amount = false) {
    const money = cell?.[`${metric}_amount`];
    return <div className="min-w-0 break-words">{loss(cell?.[metric])}
      {amount && money != null && <p className="text-muted-foreground">{formatMoney((-Number(money)).toFixed(2), data?.base_currency ?? "USD")}</p>}
    </div>;
  }
  const rows = [
    ["var", selected?.daily, selected?.monthly, "var", true],
    ["cvar", selected?.daily, selected?.monthly, "cvar", true],
    ["benchmarkVar", selected?.benchmark_daily, selected?.benchmark_monthly, "var", false],
    ["benchmarkCvar", selected?.benchmark_daily, selected?.benchmark_monthly, "cvar", false],
  ] as const;
  return <Card className="min-w-0">
    <CardHeader><CardTitle>{t("title")}</CardTitle></CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-4 px-4 text-sm">
      <p>{t("intro")}</p>
      <CalculatingOverlay active={pending} label={common("calculating")}>
        <div className="flex min-w-0 flex-col gap-4">
          <div role="radiogroup" aria-label={t("levelLabel")} className="flex flex-wrap items-center gap-1">
            {([95, 99] as const).map(value => <Button key={value} type="button" size="sm" disabled={pending} aria-pressed={value === level}
              variant={value === level ? "default" : "outline"} onClick={() => setLevel(value)}>{t(`levels.${value}`)}</Button>)}
          </div>
          <div data-testid="tail-table" className="grid min-w-0 grid-cols-3 gap-x-2 gap-y-3 text-xs sm:text-sm">
            <span className="min-w-0 break-words" /><span className="min-w-0 break-words">{t("columns.day")}</span><span className="min-w-0 break-words">{t("columns.month")}</span>
            {rows.map(([key, daily, monthly, metric, amount]) => <div key={key} className="contents">
              <span className="min-w-0 break-words">{t(`rows.${key}`, { benchmark: benchmarkLabel })}</span>
              {valueCell(daily, metric, amount)}{valueCell(monthly, metric, amount)}
            </div>)}
            <div className="contents"><span className="min-w-0 break-words">{t("rows.reference")}</span>
              {ok && data.tolerance_status === "no_questionnaire" ? <div className="col-span-2 min-w-0 break-words">{t("noQuestionnaire")} <Link className="underline" href="/questionnaire">{t("questionnaireLink")}</Link></div>
                : <><div className="min-w-0 break-words">{loss(selected?.reference_daily)}</div><div className="min-w-0 break-words">{loss(selected?.reference_monthly)}</div></>}
            </div>
          </div>
          {ok && level === 99 && !selected?.available && <p>{t("insufficient99", { count: data.sample_count })}</p>}
          <div data-testid="tail-histogram" className="h-48 w-full min-w-0">
            {ok && <ResponsiveContainer width="100%" height="100%"><BarChart data={data.histogram.map(bin => ({ mid: (Number(bin.lower) + Number(bin.upper)) / 2, count: bin.count }))}>
              <XAxis type="number" dataKey="mid" domain={["dataMin", "dataMax"]} tickFormatter={formatTickPct} tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} />
              <YAxis hide />
              <Bar dataKey="count" fill="var(--muted-foreground)" fillOpacity={0.6} isAnimationActive={false} />
              {selected?.daily && <><ReferenceLine x={-Number(selected.daily.var)} stroke={PORTFOLIO_COLOR} /><ReferenceLine x={-Number(selected.daily.cvar)} stroke={PORTFOLIO_COLOR} strokeDasharray="6 3" /></>}
              {selected?.reference_daily != null && <ReferenceLine x={-Number(selected.reference_daily)} stroke="var(--foreground)" strokeDasharray="2 3" />}
            </BarChart></ResponsiveContainer>}
            {data && !ok && <div className="flex h-full items-center justify-center"><p>{replay(data.status === "no_holdings" ? "noHoldings" : data.status)}</p></div>}
          </div>
          {ok && <div className="flex flex-wrap gap-3 text-xs">
            {selected?.daily && <><span style={{ color: PORTFOLIO_COLOR }}>{t("legend.var")}</span><span style={{ color: PORTFOLIO_COLOR }}>{t("legend.cvar")}</span></>}
            {selected?.reference_daily != null && <span>{t("legend.reference")}</span>}
          </div>}
          {data && (ok || data.status === "insufficient") && <>
            {data.coverage.data_quality && <p>{replay("dataQuality")}</p>}
            {data.proxy_understates && <p>{t("proxyUnderstates")}</p>}
          </>}
          <details className="min-w-0 break-words"><summary className="cursor-pointer font-medium">{t("model")}</summary>
            <div className="mt-3 flex min-w-0 flex-col gap-3">
              {data && (ok || data.status === "insufficient") && <><p>{t("window", { start: data.window_start, end: data.window_end, count: data.sample_count, windows: data.month_windows, independent: data.month_independent })}</p><p>{coverage}</p></>}
              <p>{t("methodText", { currency: data?.base_currency ?? currency ?? "USD" })}</p>
              {ok && selected?.available && <p>{t("normalLine", { level, dayVar: loss(selected.normal_daily?.var), monthVar: loss(selected.normal_monthly?.var), dayCvar: loss(selected.normal_daily?.cvar), monthCvar: loss(selected.normal_monthly?.cvar) })}</p>}
              <p>{t("referenceText")}</p><p>{t("limitations")}</p>
            </div>
          </details>
        </div>
      </CalculatingOverlay>
      {error && <p role="alert" className="text-destructive">{t("loadError")}</p>}
    </CardContent>
  </Card>;
}
