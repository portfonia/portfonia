"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useLocale, useLocalizedHref } from "@/app/_components/locale-provider";
import { CurrencySwitcher } from "@/app/portfolio/_components/currency-switcher";
import type { BaseCurrency } from "@/app/portfolio/_components/currencies";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { CalculatingOverlay } from "@/components/calculating-overlay";
import { ReplayRangeTabs } from "../_components/replay-range-tabs";
import { getPortfolioSummary, calculateJadeWhatIf, WhatIfCooldownError, type Me, type WhatIfRow, type RebalanceRule, type ReplayRange, type JadeWhatIf, type HoldingValueOut } from "@/lib/api";
import { WhatIfResults } from "./what-if-results";

const symbols = ["SPY", "QQQ", "EFA", "EEM", "2800.HK", "510300.SS", "ISF.L", "EXSA.DE", "1306.T", "069500.KS", "AGG", "GLD"];
type Row = WhatIfRow & { name: string; id: string };
type Version = { rule: RebalanceRule; rows: Row[] };
function cents(value: string): bigint {
  const plain = /e/i.test(value) ? Number(value).toLocaleString("en", { useGrouping: false, maximumFractionDigits: 2 }) : value;
  const [whole, fraction = ""] = (plain || "0").split(".");
  return BigInt(whole || "0") * BigInt(100) + BigInt(fraction.padEnd(2, "0").slice(0, 2)) * (value.startsWith("-") ? -BigInt(1) : BigInt(1));
}
function total(rows: Row[]) { return rows.reduce((sum, row) => sum + cents(row.amount), BigInt(0)); }
function tenths(amount: bigint, base: bigint) { return base > BigInt(0) ? Number((amount * BigInt(1000) + base / BigInt(2)) / base) : null; }

export function WhatIfBody({ me, hadLoadError }: { me: Me | null; hadLoadError: boolean }) {
  const t = useTranslations("jade.whatIf"), replay = useTranslations("jade.replay"), profile = useTranslations("profile"), common = useTranslations("common");
  const { locale } = useLocale(); const href = useLocalizedHref();
  const isJade = me?.subscription.status === "active" && me.subscription.type === "jade";
  const [currency, setCurrency] = useState<BaseCurrency>((me?.report_currency ?? "USD") as BaseCurrency);
  const [range, setRange] = useState<ReplayRange>("1Y");
  const [current, setCurrent] = useState<Row[]>([]), [excluded, setExcluded] = useState<HoldingValueOut[]>([]);
  const [versions, setVersions] = useState<Version[]>([]), [selected, setSelected] = useState(0);
  const [loading, setLoading] = useState(true), [pending, setPending] = useState(false), [error, setError] = useState(false);
  const [data, setData] = useState<JadeWhatIf | null>(null), [seconds, setSeconds] = useState(0), [confirm, setConfirm] = useState(false);
  useEffect(() => {
    if (!isJade) return;
    let active = true;
    getPortfolioSummary(currency).then(snapshot => {
      if (!active) return;
      const valued = snapshot.holdings.filter(h => Number(h.market_value_base) > 0);
      const rows: Row[] = valued.map(h => ({ id: h.holding_id, kind: "holding", holding_id: h.holding_id, name: h.name, amount: h.market_value_base ?? "0" }));
      setCurrent(rows); setExcluded(snapshot.holdings.filter(h => !valued.includes(h)));
      setVersions([{ rule: "none", rows: rows.map(row => ({ ...row })) }, { rule: "none", rows: rows.map(row => ({ ...row })) }]);
      setSelected(0);
    }).catch(() => { if (active) setError(true); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [currency, isJade]);
  useEffect(() => {
    if (seconds <= 0) return;
    const timer = setTimeout(() => setSeconds(value => Math.max(0, value - 1)), 1000);
    return () => clearTimeout(timer);
  }, [seconds]);
  const money = (value: bigint, signed = false) => new Intl.NumberFormat(locale, { style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: 2, ...(signed ? { signDisplay: "always" as const } : {}) }).format(Number(value) / 100);
  const pct = (value: number | null) => value === null ? "—" : new Intl.NumberFormat(locale, { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(value / 1000);
  const base = total(current), rows = selected === 0 ? current : versions[selected - 1]?.rows ?? [], sum = total(rows);
  const outside = versions.map((version, i) => ({ i, ratio: tenths(total(version.rows), base) })).filter(v => v.ratio !== null && (v.ratio < 990 || v.ratio > 1010));
  const names = [t("columns.current"), t("columns.a"), t("columns.b")];
  function edit(next: Version) { setVersions(values => values.map((value, i) => i === selected - 1 ? next : value)); }
  async function submit() {
    if (pending || seconds > 0 || loading) return;
    setPending(true); setError(false); setConfirm(false);
    try { setData(await calculateJadeWhatIf({ base_currency: currency, range, versions: versions.map(version => ({ rule: version.rule, rows: version.rows.map(({ kind, holding_id, symbol, amount }) => ({ kind, holding_id, symbol, amount })) })) })); }
    catch (err) { if (err instanceof WhatIfCooldownError) setSeconds(err.seconds); else setError(true); }
    finally { setPending(false); }
  }
  if (hadLoadError || !me) return <p role="alert">{profile("errorLoadFailed")}</p>;
  if (!isJade) return <Card><CardContent className="space-y-3 pt-6"><p>{t("access")}</p><Link className="underline" href={href("/jade")}>{t("jadeLink")}</Link></CardContent></Card>;
  return <div className="flex min-w-0 flex-col gap-6">
    <Card><CardHeader><h1 className="font-heading text-2xl">{t("title")}</h1></CardHeader><CardContent className="space-y-3 text-sm"><p>{t("intro")}</p><Link className="underline" href={href("/guides/jade-what-if")}>{t("guideLink")}</Link></CardContent></Card>
    <div className="flex flex-wrap gap-3"><CurrencySwitcher value={currency} disabled={pending || loading} onChange={value => { if (value === currency) return; setLoading(true); setError(false); setCurrent([]); setVersions([]); setCurrency(value); }} /><p className="text-sm text-muted-foreground">{t("currencyHint")}</p></div>
    {loading && <p role="status">{common("calculating")}</p>}
    {!loading && base === BigInt(0) && !error && <p>{t("noHoldings")} <Link className="underline" href={href("/holdings")}>{t("holdingsLink")}</Link></p>}
    {!loading && base > BigInt(0) && <>
      <Card className="min-w-0"><CardContent className="flex min-w-0 flex-col gap-4 pt-6">
        <div className="flex flex-wrap gap-2">{names.map((name, i) => <Button key={i} variant={selected === i ? "default" : "outline"} aria-pressed={selected === i} onClick={() => setSelected(i)} disabled={pending}>{name}</Button>)}</div>
        {selected > 0 && <label className="flex flex-wrap items-center gap-2 text-sm">{t("rule")}<select aria-label={t("rule")} className="rounded-md border bg-background p-2" disabled={pending} value={versions[selected - 1].rule} onChange={event => edit({ ...versions[selected - 1], rule: event.target.value as RebalanceRule })}>{(["none", "monthly", "quarterly"] as const).map(rule => <option key={rule} value={rule}>{t(`rules.${rule}`)}</option>)}</select></label>}
        <div className="flex min-w-0 flex-col gap-3">{rows.map(row => <div key={row.id} data-testid={`row-${row.id}`} className="flex min-w-0 flex-wrap items-center gap-2 rounded-md border p-3 text-sm">
          <label htmlFor={`amount-${row.id}`} className="min-w-0 flex-1 basis-36 break-words">{row.name}</label>
          {selected === 0 ? <span>{money(cents(row.amount))}</span> : <input id={`amount-${row.id}`} aria-label={row.name} type="number" min="0" step="0.01" className="w-36 max-w-full rounded border bg-background p-2" value={row.amount} disabled={pending} onChange={event => edit({ ...versions[selected - 1], rows: rows.map(value => value.id === row.id ? { ...value, amount: event.target.value } : value) })} />}
          <span>{pct(tenths(cents(row.amount), sum))}</span>
          {selected > 0 && row.kind !== "holding" && <Button variant="outline" size="sm" disabled={pending} onClick={() => edit({ ...versions[selected - 1], rows: rows.filter(value => value.id !== row.id) })}>{t("remove")}</Button>}
        </div>)}</div>
        {selected > 0 && <label className="flex flex-wrap items-center gap-2 text-sm">{t("add")}<select aria-label={t("add")} className="max-w-full rounded border bg-background p-2" disabled={pending} value="" onChange={event => { const symbol = event.target.value; if (!symbol) return; edit({ ...versions[selected - 1], rows: [...rows, symbol === "cash" ? { id: "cash", kind: "cash", name: t("cash"), amount: "0" } : { id: `etf-${symbol}`, kind: "etf", symbol, name: symbol, amount: "0" }] }); }}><option value="">{t("add")}</option>{symbols.filter(symbol => !rows.some(row => row.kind === "etf" && row.symbol === symbol)).map(symbol => <option key={symbol} value={symbol}>{symbol}</option>)}{!rows.some(row => row.kind === "cash") && <option value="cash">{t("cash")}</option>}</select></label>}
        <dl className="grid min-w-0 grid-cols-[1fr_auto] gap-2 text-sm"><dt>{t("total")}</dt><dd data-testid="editor-total">{money(sum)}</dd><dt>{t("ratio")}</dt><dd data-testid="editor-ratio">{pct(tenths(sum, base))}</dd><dt>{t("difference")}</dt><dd data-testid="editor-difference">{money(sum - base, true)}</dd></dl>
        {excluded.length > 0 && <div className="text-sm"><p>{t("notIncluded")}</p><ul>{excluded.map(row => <li key={row.holding_id}>{row.name}</li>)}</ul></div>}
      </CardContent></Card>
      <details data-testid="etf-list" className="min-w-0 text-sm"><summary className="cursor-pointer">{t("etfList")}</summary><ul className="mt-3 space-y-2">{symbols.map(symbol => <li key={symbol} data-symbol={symbol}>{symbol} — {t(`etfs.${symbol.replaceAll(".", "_")}`)}</li>)}</ul></details>
      <ReplayRangeTabs value={range} onChange={setRange} disabled={pending} />
      <Button className="self-start" disabled={pending || seconds > 0 || loading} onClick={() => outside.length ? setConfirm(true) : void submit()}>{t("calculate")}</Button>
    </>}
    {seconds > 0 && <p role="status" data-testid="cooldown">{t("cooldown")} {t("countdown", { seconds })}</p>}
    {error && <p role="alert" className="text-destructive">{replay("loadError")}</p>}
    <div className="space-y-2 text-sm text-muted-foreground"><p>{t("hindsight")}</p><p>{t("replayAnchoring")}</p><p>{t("stressAnchoring")}</p><p>{t("notModelled")}</p><p>{t("watermark")}</p></div>
    <CalculatingOverlay active={pending} label={common("calculating")}><div className="min-h-24 min-w-0">{data && <WhatIfResults data={data} />}</div></CalculatingOverlay>
    <AlertDialog open={confirm} onOpenChange={setConfirm}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t("confirmTitle")}</AlertDialogTitle><AlertDialogDescription>{t("confirmRatios", { versions: outside.map(v => `${names[v.i + 1]}: ${pct(v.ratio)}`).join(t("listSeparator")) })}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>{t("cancel")}</AlertDialogCancel><AlertDialogAction onClick={() => void submit()}>{t("confirm")}</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </div>;
}
