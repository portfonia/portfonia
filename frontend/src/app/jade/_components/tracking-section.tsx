"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { CalculatingOverlay } from "@/components/calculating-overlay";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useLocale, useLocalizedHref } from "@/app/_components/locale-provider";
import type { BaseCurrency } from "@/app/portfolio/_components/currencies";
import { formatShortDate, formatTickPct } from "@/app/portfolio/performance/_components/performance-format";
import { PORTFOLIO_COLOR } from "@/app/portfolio/performance/_components/performance-colors";
import { getJadeTracking, saveJadeBenchmark, type BenchmarkCode, type JadeBenchmark, type JadeTracking, type ReplayRange, type TrackingComponent } from "@/lib/api";

type Settings = { currency: BaseCurrency; range: ReplayRange; benchmark: BenchmarkCode };
type RequestSettings = Pick<Settings, "currency" | "range">;
type Row = { symbol: string; weight: string };
const symbols = ["SPY", "QQQ", "DIA", "ONEQ", "IWF", "IWD", "IWM", "ACWI", "EFA", "EEM", "2800.HK", "510300.SS", "ISF.L", "EXSA.DE", "1306.T", "069500.KS", "AGG", "TLT", "BIL", "GLD", "DBC", "VNQ", "XLE", "CASH"];
const metricRows = [["portfolio", "portfolio_return"], ["reference", "benchmark_return"], ["excess", "excess_return"], ["annualizedExcess", "annualized_excess"], ["trackingError", "tracking_error"]] as const;
function matches(settings: RequestSettings, data: JadeTracking | null) {
  return data?.base_currency === settings.currency && data?.range === settings.range;
}
function editorRows(components: TrackingComponent[]): Row[] {
  return components.map(c => ({ symbol: c.symbol, weight: String(c.weight) }));
}
export function TrackingSection({ settings }: { settings: Settings | null }) {
  const t = useTranslations("jade.tracking");
  const replay = useTranslations("jade.replay");
  const common = useTranslations("common");
  const { locale } = useLocale();
  const href = useLocalizedHref();
  const jade = useTranslations("jade");
  const [data, setData] = useState<JadeTracking | null>(null);
  const [pending, setPending] = useState(true);
  const [error, setError] = useState(false);
  const [reference, setReference] = useState<JadeBenchmark | null>(null);
  const savedVersion = useRef(0);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(false);
  const latestSettings = useRef<RequestSettings | null>(null);
  const savedData = useRef<JadeTracking | null>(null);
  const busy = useRef(false);
  const reload = useRef(false);
  const mounted = useRef(false);
  const request = useRef<(settings: RequestSettings | null) => void>(() => {});
  useEffect(() => {
    let alive = true;
    mounted.current = true;
    function load(next: RequestSettings | null) {
      busy.current = true;
      setPending(true); setError(false);
      const version = savedVersion.current;
      const promise = next ? getJadeTracking(next.currency, next.range) : getJadeTracking();
      promise.then(result => {
        if (alive) {
          savedData.current = result;
          setData(result);
          if (version === savedVersion.current) setReference({ components: result.components, saved: result.benchmark_saved });
          setRows(previous => previous ?? editorRows(result.components));
        }
      }).catch(() => { if (alive) setError(true); }).finally(() => {
        busy.current = false;
        if (!alive) return;
        setPending(false);
        const latest = latestSettings.current;
        if (reload.current) {
          reload.current = false;
          load(latest);
        } else if (latest && latest !== next && !matches(latest, savedData.current)) load(latest);
      });
    }
    request.current = load;
    load(null);
    return () => { alive = false; mounted.current = false; };
  }, []);
  const currency = settings?.currency;
  const range = settings?.range;
  useEffect(() => {
    if (!currency || !range) return;
    const next = { currency, range };
    latestSettings.current = next;
    if (!busy.current && !matches(next, savedData.current)) request.current(next);
  }, [currency, range]);

  const total = rows?.reduce((sum, r) => sum + (Number(r.weight) || 0), 0) ?? 0;
  const valid = rows !== null && rows.length >= 1 && rows.length <= 5 &&
    rows.every(r => r.weight.trim() !== "" && Number.isInteger(Number(r.weight)) && Number(r.weight) >= 1 && Number(r.weight) <= 100 && symbols.includes(r.symbol)) &&
    new Set(rows.map(r => r.symbol)).size === rows.length && total === 100;
  const unchanged = rows !== null && JSON.stringify(rows.map(r => ({ symbol: r.symbol, weight: Number(r.weight) }))) === JSON.stringify(reference?.components);
  async function save() {
    if (!rows || !valid || unchanged || saving) return;
    setSaving(true); setSaveError(false);
    try {
      const result = await saveJadeBenchmark(rows.map(r => ({ symbol: r.symbol, weight: Number(r.weight) })));
      if (!mounted.current) return;
      savedVersion.current += 1;
      setReference(result);
      setRows(editorRows(result.components));
      if (busy.current) reload.current = true;
      else request.current(latestSettings.current);
    } catch { if (mounted.current) setSaveError(true); }
    finally { if (mounted.current) setSaving(false); }
  }
  function updateRow(index: number, change: Partial<Row>) {
    setRows(previous => previous?.map((row, i) => i === index ? { ...row, ...change } : row) ?? null);
  }
  const displayCurrency = data?.base_currency ?? currency ?? "USD";
  const label = (symbol: string) => t(`labels.${symbol.replaceAll(".", "_")}`, { currency: displayCurrency });
  const composition = data?.components.map(c => `${c.weight}% ${label(c.symbol)}${c.symbol === "CASH" ? "" : ` (${c.symbol})`}`).join(" + ") ?? "";
  const metrics = data?.metrics;
  const ok = data?.status === "ok" && data.benchmark_status === "ok";
  const chart = data?.points.map(p => ({ time: new Date(`${p.date}T00:00:00Z`).getTime(), excess: Number(p.excess) })) ?? [];
  return <Card className="min-w-0">
    <CardHeader><CardTitle>{t("title")}</CardTitle></CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-4 px-4 text-sm">
      <p>{t("intro")}</p>
      <p className="min-w-0 break-words">{t("whatItSays")}</p>
      <Link className="underline" href={href("/guides/jade-tracking")}>{jade("guideLink")}</Link>
      {rows && <div data-testid="tracking-editor" className="flex min-w-0 flex-col gap-3">
        <h3 className="font-medium">{t("editor.title")}</h3>
        {!reference?.saved && <p>{t("editor.example")}</p>}
        {rows.map((row, index) => <div key={index} data-tracking-row className="flex min-w-0 flex-col gap-2">
          <label className="min-w-0">{t("editor.component")}
            <select aria-label={`${t("editor.component")} ${index + 1}`} value={row.symbol} onChange={event => updateRow(index, { symbol: event.target.value })} className="w-full min-w-0 rounded-md border border-input bg-background px-2 py-1.5 text-foreground">
              {symbols.map(symbol => <option key={symbol} value={symbol} disabled={rows.some((other, i) => i !== index && other.symbol === symbol)}>{label(symbol)}{symbol !== "CASH" && ` (${symbol})`}</option>)}
            </select>
          </label>
          <div className="flex min-w-0 items-end gap-2">
            <label className="min-w-0 flex-1">{t("editor.weight")}<Input aria-label={`${t("editor.weight")} ${index + 1}`} type="number" min={1} max={100} step={1} inputMode="numeric" value={row.weight} onChange={event => updateRow(index, { weight: event.target.value })} className="w-full min-w-0" /></label>
            {rows.length > 1 && <Button type="button" variant="outline" onClick={() => setRows(rows.filter((_, i) => i !== index))}>{t("editor.remove")}</Button>}
          </div>
        </div>)}
        {rows.length < 5 && <Button className="self-start" type="button" variant="outline" onClick={() => { const symbol = symbols.find(s => !rows.some(r => r.symbol === s)); if (symbol) setRows([...rows, { symbol, weight: "" }]); }}>{t("editor.add")}</Button>}
        <p>{t("editor.total", { total })}</p>
        {!valid && <p>{t("editor.invalid")}</p>}
        <Button type="button" className="self-start" disabled={!valid || unchanged || saving} onClick={() => void save()}>{t(saving ? "editor.saving" : "editor.save")}</Button>
        {saveError && <p role="alert" className="text-destructive">{t("editor.saveError")}</p>}
      </div>}
      <CalculatingOverlay active={pending} label={common("calculating")}>
        <div className="flex min-w-0 flex-col gap-4">
          {data && <p className="min-w-0 break-words">{t("referenceLabel", { composition })}</p>}
          <div data-testid="tracking-table" className="grid min-w-0 grid-cols-2 gap-x-2 gap-y-3 text-xs sm:text-sm">
            {metricRows.map(([name, key]) => <div className="contents" key={key}><span className="min-w-0 break-words">{t(`rows.${name}`)}</span><span className="min-w-0 break-words">{metrics?.[key] == null ? "—" : formatTickPct(Number(metrics[key]))}</span></div>)}
          </div>
          <div data-testid="tracking-chart" className="h-60 w-full min-w-0">
            {ok && <ResponsiveContainer width="100%" height="100%"><LineChart data={chart} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
              <XAxis type="number" dataKey="time" domain={["dataMin", "dataMax"]} tickFormatter={(v: number) => formatShortDate(new Date(v).toISOString().slice(0, 10), locale)} tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} minTickGap={24} />
              <YAxis tickFormatter={formatTickPct} tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} width={48} />
              <Tooltip labelFormatter={v => formatShortDate(new Date(Number(v)).toISOString().slice(0, 10), locale)} formatter={v => typeof v === "number" ? formatTickPct(v) : "—"} contentStyle={{ background: "var(--card)", borderColor: "var(--border)" }} />
              <ReferenceLine y={0} stroke="var(--muted-foreground)" strokeDasharray="3 3" />
              <Line dataKey="excess" name={t("legend.excess")} stroke={PORTFOLIO_COLOR} strokeWidth={2.5} dot={false} isAnimationActive={false} />
            </LineChart></ResponsiveContainer>}
          </div>
          {ok && <span style={{ color: PORTFOLIO_COLOR }}>{t("legend.excess")}</span>}
          {metrics && data && <>{["1M", "3M", "6M", "YTD"].includes(data.range) && <p>{replay("annualizedShortNote")}</p>}{metrics.tracking_error === null && <p>{t("teInsufficient", { count: data.sample_count, min: data.min_samples })}</p>}</>}
          {data && (data.status === "ok" || data.status === "insufficient") && <>{data.coverage.data_quality && <p>{replay("dataQuality")}</p>}{data.proxy_note && <p>{t("proxyNote")}</p>}</>}
          {data && data.status !== "ok" && <p>{replay(data.status === "no_holdings" ? "noHoldings" : data.status)}</p>}
          {data?.benchmark_status === "pending" && <p>{t("referencePending")}</p>}
          {data?.benchmark_status === "unavailable" && <p>{t("referenceUnavailable")}</p>}
          <details className="min-w-0 break-words"><summary className="cursor-pointer font-medium">{t("model")}</summary><div className="mt-3 flex min-w-0 flex-col gap-3">
            {metrics && data && <p>{t("windowText", { start: data.window_start, end: data.window_end, count: data.sample_count })}</p>}
            <p>{t("methodText", { currency: displayCurrency })}</p><p>{t("limitations")}</p>
          </div></details>
        </div>
      </CalculatingOverlay>
      {error && <p role="alert" className="text-destructive">{t("loadError")}</p>}
    </CardContent>
  </Card>;
}
