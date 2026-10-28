"use client";

import { useEffect, useState, useTransition } from "react";
import { useTranslations } from "next-intl";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useLocale } from "@/app/_components/locale-provider";
import { formatAxisDate } from "@/app/portfolio/performance/_components/performance-format";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { getTechnicalSeries, getTechnicalTable, type TechnicalSeriesOut, type TechnicalTableOut } from "@/lib/api";

const metrics = ["pct_vs_sma50", "pct_vs_sma200", "pct_in_52w_range", "vol_20d_annualized"] as const;
const labels = ["sma50", "sma200", "range", "volatility"] as const;
// The #824 chart tokens are verified at >= 3:1 on the dark card surface.
const colors = ["var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)"];
function todayET(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}
function precedingDate(end: string): string {
  const day = new Date(`${end}T12:00:00Z`);
  day.setUTCDate(day.getUTCDate() - 91);
  return day.toISOString().slice(0, 10);
}
function percent(value: number | null, whole = false): string {
  if (value === null) return "—";
  return `${!whole && value >= 0 ? "+" : ""}${(value * 100).toFixed(whole ? 0 : 1)}%`;
}

export function TechnicalPageBody({ initialTable, initialLoadError }: { initialTable: TechnicalTableOut | null; initialLoadError: boolean }) {
  const { locale } = useLocale();
  const t = useTranslations("portfolio.technical");
  const today = todayET();
  const [table, setTable] = useState(initialTable);
  const [date, setDate] = useState("");
  const [tableError, setTableError] = useState(initialLoadError);
  const [pending, startTransition] = useTransition();
  const [ticker, setTicker] = useState(initialTable?.positions[0]?.ticker ?? "");
  const [start, setStart] = useState(precedingDate(today));
  const [end, setEnd] = useState(today);
  const [series, setSeries] = useState<TechnicalSeriesOut | null>(null);
  const [seriesError, setSeriesError] = useState(false);
  const days = (Date.parse(end) - Date.parse(start)) / 86400000;
  const tooLong = days > 91;
  const invalid = !start || !end || start > end || end > today;

  useEffect(() => {
    if (!ticker || tooLong || invalid) return;
    let active = true;
    getTechnicalSeries(ticker, start, end).then(result => {
      if (active) { setSeries(result); setSeriesError(false); }
    }).catch(() => { if (active) { setSeries(null); setSeriesError(true); } });
    return () => { active = false; };
  }, [ticker, start, end, tooLong, invalid]);

  function changeDate(value: string) {
    setDate(value);
    startTransition(async () => {
      try {
        const result = await getTechnicalTable(value || undefined);
        setTable(result);
        setTicker(result.positions[0]?.ticker ?? "");
        setTableError(false);
      } catch { setTableError(true); }
    });
  }
  const displayedSeries = series?.ticker === ticker && series.start === start && series.end === end ? series : null;
  const points = displayedSeries?.points;
  const spanDays = points && points.length >= 2 ? (Date.parse(points[points.length - 1].date) - Date.parse(points[0].date)) / 86_400_000 : 0;
  return <div className="space-y-8">
    <h1 className="font-serif text-2xl">{t("title")}</h1>
    <Card>
      <CardHeader><CardTitle>{t("tableTitle")}</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <label className="flex flex-col gap-1 text-sm">{t("date")}<input type="date" max={today} value={date} disabled={pending} onChange={event => changeDate(event.target.value)} className="w-fit rounded border border-border bg-background p-2" /></label>
        {tableError ? <p role="alert" className="text-sm text-destructive">{t("loadError")}</p> : table && <>
          <p className="text-sm text-muted-foreground">{t("asOf", { date: table.as_of })}</p>
          {table.holdings_date && table.holdings_date !== (date || table.as_of) && <p className="text-sm text-muted-foreground">{t("holdingsAsOf", { date: table.holdings_date })}</p>}
          {!table.holdings_date ? <p>{t("noSnapshot")}</p> : table.positions.length === 0 ? <p>{t("noHoldings")}</p> : <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr>{["holding", ...labels].map(key => <th key={key} className="p-2 text-left font-medium">{t(key)}</th>)}</tr></thead><tbody>{table.positions.map(row => <tr key={row.ticker} className="border-t border-border"><td className="p-2">{row.name}<span className="block text-xs text-muted-foreground">{row.ticker}</span></td>{metrics.map((key, index) => <td key={key} className="p-2 tabular-nums">{percent(row[key], index === 2)}</td>)}</tr>)}</tbody></table></div>}
          <p className="text-xs text-muted-foreground">{t("rangeNote")}</p>
        </>}
      </CardContent>
    </Card>
    <Card>
      <CardHeader><CardTitle>{t("seriesTitle")}</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-4">
          <label className="flex flex-col gap-1 text-sm">{t("holding")}<select value={ticker} onChange={event => setTicker(event.target.value)} className="rounded border border-border bg-background p-2">{table?.positions.map(row => <option key={row.ticker} value={row.ticker}>{row.name} ({row.ticker})</option>)}</select></label>
          <label className="flex flex-col gap-1 text-sm">{t("start")}<input type="date" max={today} value={start} onChange={event => setStart(event.target.value)} className="rounded border border-border bg-background p-2" /></label>
          <label className="flex flex-col gap-1 text-sm">{t("end")}<input type="date" max={today} value={end} onChange={event => setEnd(event.target.value)} className="rounded border border-border bg-background p-2" /></label>
        </div>
        {tooLong ? <p role="alert" className="text-sm text-destructive">{t("rangeTooLong")}</p> : invalid ? <p role="alert" className="text-sm text-destructive">{t("invalidRange")}</p> : seriesError ? <p role="alert" className="text-sm text-destructive">{t("loadError")}</p> : displayedSeries?.points.length ? <div><div className="h-80"><ResponsiveContainer width="100%" height="100%"><LineChart data={displayedSeries.points}><CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} /><XAxis dataKey="date" tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} tickLine={false} axisLine={false} minTickGap={24} tickFormatter={value => formatAxisDate(String(value), locale, spanDays)} /><YAxis tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} tickLine={false} axisLine={false} width={64} tickFormatter={value => `${Math.round(Number(value) * 100)}%`} /><Tooltip contentStyle={{ background: "var(--card)", borderColor: "var(--border)" }} formatter={value => percent(value === undefined ? null : Number(value))} />{metrics.map((key, index) => <Line key={key} dataKey={key} name={t(labels[index])} stroke={colors[index]} strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />)}</LineChart></ResponsiveContainer></div><ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">{metrics.map((key, index) => <li key={key} className="flex items-center gap-2"><span aria-hidden="true" className="h-0.5 w-4 rounded-full" style={{ backgroundColor: colors[index] }} />{t(labels[index])}</li>)}</ul></div> : displayedSeries && <p>{t("noPoints")}</p>}
      </CardContent>
    </Card>
  </div>;
}
