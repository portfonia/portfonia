"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, XAxis, YAxis } from "recharts";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { BENCHMARK_CODES, getPortfolioRisk, type BenchmarkCode, type PortfolioRiskResponse } from "@/lib/api";
import { BenchmarkSingleSelectMenu } from "../performance/_components/benchmark-single-select-menu";

function pct(value: string | null): string {
  return value === null ? "—" : `${(Number(value) * 100).toFixed(1)}%`;
}

export function betaGaugePosition(value: number): { position: number; segment: "green" | "gold" | "red" } {
  return {
    position: Math.max(0, Math.min(100, (value / 3) * 100)),
    segment: value < 1 ? "green" : value < 2 ? "gold" : "red",
  };
}

function ExplanationCell({
  name,
  value,
  subtitle,
  explanation,
  children,
}: {
  name: string;
  value: string;
  subtitle: string;
  explanation: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const t = useTranslations("portfolio.riskPanel");
  return (
    <button
      type="button"
      aria-label={`${name}: ${value}. ${explanation}`}
      aria-expanded={open}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      onClick={() => setOpen(true)}
      className="min-w-0 rounded-xl border border-border bg-background/30 p-4 text-left transition-colors hover:border-primary/50 hover:bg-primary/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
    >
      <div className="flex items-center justify-between text-sm font-semibold">
        <span>{name}</span><span aria-hidden="true" className="rounded-full border border-current px-1.5 text-xs">{t("infoSymbol")}</span>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{subtitle}</p>
      {children}
      {open && <p role="tooltip" className="mt-3 border-t border-border pt-3 text-xs leading-relaxed text-muted-foreground">{explanation}</p>}
    </button>
  );
}

function BetaGauge({ value }: { value: number | null }) {
  const t = useTranslations("portfolio.riskPanel");
  if (value === null) return null;
  const gauge = betaGaugePosition(value);
  const theta = Math.PI * (1 - gauge.position / 100);
  const x = 100 + 66 * Math.cos(theta);
  const y = 86 - 66 * Math.sin(theta);
  return (
    <div className="relative mx-auto mt-4 max-w-56">
      <svg viewBox="0 0 200 115" className="w-full" aria-hidden="true">
        <path d="M 20 86 A 80 80 0 0 1 60 16.7" fill="none" stroke="#4eaa83" strokeWidth="12" />
        <path d="M 60 16.7 A 80 80 0 0 1 140 16.7" fill="none" stroke="#dab55a" strokeWidth="12" />
        <path d="M 140 16.7 A 80 80 0 0 1 180 86" fill="none" stroke="#a15b42" strokeWidth="12" />
        <line x1="100" y1="86" x2={x} y2={y} stroke="currentColor" strokeWidth="2" />
        <circle cx="100" cy="86" r="4" fill="currentColor" />
      </svg>
      <div className="absolute inset-x-0 bottom-4 text-center font-heading text-3xl tabular-nums">{value.toFixed(2)}</div>
      <div className="flex justify-between text-xs text-muted-foreground"><span>{t("betaZero")}</span><span>{t("betaThree")}</span></div>
    </div>
  );
}

function Scale({ position, labels, marker }: { position: number | null; labels: [string, string, string]; marker: "triangle" | "line" }) {
  return (
    <div className="mt-5">
      <div className="relative flex h-2 overflow-visible rounded-full bg-gradient-to-r from-emerald-500 via-amber-400 to-orange-700">
        {position !== null && (
          <span
            aria-hidden="true"
            className={`absolute top-[-7px] h-5 w-0.5 bg-foreground ${marker === "triangle" ? "before:absolute before:-top-1 before:-left-1 before:border-x-[5px] before:border-t-[6px] before:border-x-transparent before:border-t-foreground" : ""}`}
            style={{ left: `${position}%` }}
          />
        )}
      </div>
      <div className="mt-3 flex justify-between gap-1 text-[10px] text-muted-foreground">
        {labels.map((label) => <span key={label}>{label}</span>)}
      </div>
    </div>
  );
}

function chartRows(data: PortfolioRiskResponse) {
  const rows = new Map<string, { date: string; portfolio: number | null; benchmark: number | null }>();
  if (data.portfolio_vol.status === "ok") {
    for (const point of data.portfolio_vol.points) rows.set(point.date, { date: point.date, portfolio: Number(point.vol) * 100, benchmark: null });
  }
  if (data.benchmark_vol.status === "ok") {
    for (const point of data.benchmark_vol.points) {
      const row = rows.get(point.date) ?? { date: point.date, portfolio: null, benchmark: null };
      row.benchmark = Number(point.vol) * 100;
      rows.set(point.date, row);
    }
  }
  return [...rows.values()].sort((a, b) => a.date.localeCompare(b.date));
}

export function RiskPanel({ baseCurrency }: { baseCurrency: string }) {
  const t = useTranslations("portfolio.riskPanel");
  const names = useTranslations("portfolio.performance.benchmarkNames");
  const [benchmark, setBenchmark] = useState<BenchmarkCode>(BENCHMARK_CODES[0]);
  const [data, setData] = useState<PortfolioRiskResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const requestSeq = useRef(0);

  useEffect(() => {
    const seq = ++requestSeq.current;
    getPortfolioRisk(benchmark, baseCurrency)
      .then((value) => { if (seq === requestSeq.current) { setData(value); setError(false); } })
      .catch(() => { if (seq === requestSeq.current) setError(true); })
      .finally(() => { if (seq === requestSeq.current) setLoading(false); });
  }, [benchmark, baseCurrency]);

  const betaValue = data?.beta.value === null || !data ? null : Number(data.beta.value);
  const betaText = betaValue === null ? t("insufficientSample") : betaValue.toFixed(2);
  const riskText = !data ? "" : data.risk.status === "ok" && data.risk.label ? t(`riskLabels.${data.risk.label}`) : t(`states.${data.risk.status}`);
  const delta = data?.deviation.delta;
  const deviationText = !data ? "" : data.deviation.status !== "ok" || delta === null || delta === undefined
    ? t(`states.${data.deviation.status}`)
    : delta > 0 ? t("aggressiveDirection", { count: delta })
      : delta < 0 ? t("conservativeDirection", { count: Math.abs(delta) }) : t("match");
  const rows = data ? chartRows(data) : [];

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
        <CardDescription>{t("subtitle")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6 px-4">
        {loading || (data !== null && data.base_currency !== baseCurrency) ? <div role="status" aria-label={t("loading")} className="grid gap-4 min-[461px]:grid-cols-2 min-[721px]:grid-cols-[1.15fr_1fr_1fr]">
          {[0, 1, 2].map((n) => <div key={n} className="h-56 animate-pulse rounded-xl bg-muted" />)}
        </div> : error ? <p role="alert" className="text-sm text-destructive">{t("loadError")}</p> : data && <>
          <div className="grid gap-4 min-[461px]:grid-cols-2 min-[721px]:grid-cols-[1.15fr_1fr_1fr]">
            <div className="min-[461px]:col-span-2 min-[721px]:col-span-1">
              <ExplanationCell name={t("beta")} value={betaText} subtitle={t("betaSubtitle")} explanation={t("betaExplanation")}>
                {betaValue === null ? <p className="mt-5 text-xl">{betaText}</p> : <BetaGauge value={betaValue} />}
              </ExplanationCell>
            </div>
            <ExplanationCell name={t("risk")} value={riskText} subtitle={t("riskSubtitle")} explanation={t("riskExplanation")}>
              <p className="mt-7 min-h-12 font-heading text-xl text-primary">{riskText}</p>
              <Scale position={data.risk.label ? ({ within: 16.67, caution: 50, exceeds: 83.33 })[data.risk.label] : null} labels={[t("riskLabels.within"), t("riskLabels.caution"), t("riskLabels.exceeds")]} marker="triangle" />
              <p className="mt-4 text-xs text-muted-foreground">{t("objectiveTier", { tier: data.portfolio_vol.tier ? t(`tiers.${data.portfolio_vol.tier}`) : t("insufficientSample") })}</p>
            </ExplanationCell>
            <ExplanationCell name={t("deviation")} value={deviationText} subtitle={t("deviationSubtitle")} explanation={t("deviationExplanation")}>
              <p className="mt-7 min-h-12 font-heading text-xl text-primary">{deviationText}</p>
              <Scale position={delta === null || delta === undefined ? null : (delta + 2) * 25} labels={[t("conservative"), t("matchShort"), t("aggressive")]} marker="line" />
              <p className="mt-4 text-xs text-muted-foreground">{t("deviationSubline")}</p>
            </ExplanationCell>
          </div>
          <section className="min-w-0 border-t border-border pt-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h3 className="font-heading text-lg">{t("volatilityTitle")}</h3>
              <BenchmarkSingleSelectMenu label={t("benchmarkSelector")} value={benchmark} onChange={setBenchmark} />
            </div>
            <div className="mt-4 flex flex-wrap gap-5 text-sm">
              <span><span aria-hidden="true" className="mr-2 inline-block h-0.5 w-5 align-middle bg-blue-400" />{t("portfolio")}: <strong>{data.portfolio_vol.status === "ok" ? pct(data.portfolio_vol.current) : t("insufficientSample")}</strong></span>
              <span><span aria-hidden="true" className="mr-2 inline-block h-0.5 w-5 align-middle bg-amber-400" />{names(benchmark)}: <strong>{data.benchmark_vol.status === "ok" ? pct(data.benchmark_vol.current) : t("insufficientSample")}</strong></span>
            </div>
            {data.portfolio_vol.status === "ok" || data.benchmark_vol.status === "ok" ? <div role="img" aria-label={t("chartDescription", { benchmark: names(benchmark) })} className="mt-5 h-56 w-full min-w-0">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={rows} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="date" hide /><YAxis hide domain={[0, "auto"]} />
                  {data.portfolio_vol.status === "ok" && <Line dataKey="portfolio" type="monotone" stroke="#60a5fa" dot={false} connectNulls isAnimationActive={false} />}
                  {data.benchmark_vol.status === "ok" && <Line dataKey="benchmark" type="monotone" stroke="#fbbf24" dot={false} connectNulls isAnimationActive={false} />}
                </LineChart>
              </ResponsiveContainer>
            </div> : <p className="mt-5 text-sm text-muted-foreground">{t("insufficientSample")}</p>}
            <div className="flex justify-between text-xs text-muted-foreground"><span>{t("sixtyDaysAgo")}</span><span>{t("now")}</span></div>
            <p className="mt-3 text-xs text-muted-foreground">{t("sampleDisclosure", { portfolioStart: data.portfolio_vol.window_start ?? "—", portfolioEnd: data.portfolio_vol.window_end ?? "—", portfolioCount: data.portfolio_vol.sample_count, benchmarkStart: data.benchmark_vol.window_start ?? "—", benchmarkEnd: data.benchmark_vol.window_end ?? "—", benchmarkCount: data.benchmark_vol.sample_count, benchmark: names(benchmark), currency: data.base_currency })}</p>
            <p className="mt-1 text-xs text-muted-foreground">{t("manualShare", { share: pct(data.manual_valuation_share) })}</p>
          </section>
          <p className="border-t border-border pt-4 text-xs leading-relaxed text-muted-foreground">{t("footnote")}</p>
        </>}
      </CardContent>
    </Card>
  );
}
