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
import { ApiError, getPortfolioSummary, calculateJadeWhatIf, WhatIfCooldownError, type Me, type WhatIfRow, type RebalanceRule, type ReplayRange, type JadeWhatIf, type HoldingValueOut } from "@/lib/api";
import { WhatIfResults } from "./what-if-results";

const symbols = ["SPY", "QQQ", "EFA", "EEM", "2800.HK", "510300.SS", "ISF.L", "EXSA.DE", "1306.T", "069500.KS", "AGG", "GLD"];
type Row = WhatIfRow & { name: string; id: string };
type Version = { rule: RebalanceRule; rows: Row[] };
export function normalizeAmount(raw: string): string | null {
  const s = raw.trim().replaceAll(",", "").replaceAll("\uFF0C", "");
  if (s === "") return "0";
  if (s === "." || !/^\d*\.?\d*$/.test(s)) return null;
  const [whole, fraction = ""] = s.split(".");
  const truncated = fraction.slice(0, 2);
  return truncated ? `${whole || "0"}.${truncated}` : whole || "0";
}
function cents(value: string): bigint {
  if (/e/i.test(value) && !Number.isFinite(Number(value))) return BigInt(0);
  const plain = /e/i.test(value) ? Number(value).toLocaleString("en", { useGrouping: false, maximumFractionDigits: 2 }) : value;
  const [whole, fraction = ""] = (plain || "0").split(".");
  return BigInt(whole || "0") * BigInt(100) + BigInt(fraction.padEnd(2, "0").slice(0, 2)) * (value.startsWith("-") ? -BigInt(1) : BigInt(1));
}
function cellChanged(row: Row, currentAmount: string) {
  const amount = normalizeAmount(row.amount);
  return amount === null || cents(amount) !== cents(currentAmount);
}
function total(rows: Row[]) { return rows.reduce((sum, row) => sum + cents(normalizeAmount(row.amount) ?? "0"), BigInt(0)); }
function tenths(amount: bigint, base: bigint) { return base > BigInt(0) ? Number((amount * BigInt(1000) + base / BigInt(2)) / base) : null; }

export function WhatIfBody({ me, hadLoadError }: { me: Me | null; hadLoadError: boolean }) {
  const copy = useTranslations();
  const t = useTranslations("jade.whatIf"), profile = useTranslations("profile"), common = useTranslations("common");
  const { locale } = useLocale(); const href = useLocalizedHref();
  const isJade = me?.subscription.status === "active" && me.subscription.type === "jade";
  const [currency, setCurrency] = useState<BaseCurrency>((me?.report_currency ?? "USD") as BaseCurrency);
  const [range, setRange] = useState<ReplayRange>("1Y");
  const [current, setCurrent] = useState<Row[]>([]), [excluded, setExcluded] = useState<HoldingValueOut[]>([]);
  const [versions, setVersions] = useState<Version[]>([]);
  const [addedOrder, setAddedOrder] = useState<string[]>([]);
  const [loading, setLoading] = useState(true), [pending, setPending] = useState(false), [errorKey, setErrorKey] = useState<string | null>(null);
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
      setAddedOrder([]);
    }).catch(() => { if (active) setErrorKey("jade.replay.loadError"); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [currency, isJade]);
  useEffect(() => {
    if (seconds <= 0) return;
    const timer = setTimeout(() => setSeconds(value => Math.max(0, value - 1)), 1000);
    return () => clearTimeout(timer);
  }, [seconds]);
  const money = (value: bigint, signed = false) => new Intl.NumberFormat(locale, { style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: 2, ...(signed ? { signDisplay: "always" as const } : {}) }).format(Number(value) / 100);
  const pct = (value: number | null) => value === null ? "—" : new Intl.NumberFormat(locale, { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(value / 1000);
  const base = total(current);
  const currentAmount = (row: Row) => current.find(value => value.id === row.id)?.amount ?? "0";
  const versionModified = (version: Version) => version.rule !== "none" || version.rows.some(row => cellChanged(row, currentAmount(row)));
  const modified = versions.map(versionModified);
  const editorRows = [...current, ...addedOrder.flatMap(id => { const row = versions.flatMap(version => version.rows).find(row => row.id === id); return row ? [row] : []; })];
  const invalidAmount = versions.some(version => version.rows.some(row => normalizeAmount(row.amount) === null));
  const outside = versions.map((version, i) => ({ i, ratio: tenths(total(version.rows), base) })).filter(v => v.ratio !== null && (v.ratio < 990 || v.ratio > 1010));
  const names = [t("columns.current"), t("columns.a"), t("columns.b")];
  function edit(index: number, next: Version) { setVersions(values => values.map((value, i) => i === index ? next : value)); }
  async function submit() {
    if (pending || seconds > 0 || loading || invalidAmount || !modified.some(Boolean)) return;
    setPending(true); setErrorKey(null); setConfirm(false);
    try {
      const keys = (["a", "b"] as const).filter((_, i) => modified[i]);
      const response = await calculateJadeWhatIf({ base_currency: currency, range, versions: versions.filter((_, i) => modified[i]).map(version => ({ rule: version.rule, rows: version.rows.map(({ kind, holding_id, symbol, amount }) => ({ kind, holding_id, symbol, amount: normalizeAmount(amount) ?? "0" })) })) });
      setData({ ...response, columns: response.columns.map((column, i) => column.key === "current" ? column : { ...column, key: keys[i - 1] }) });
    }
    catch (err) {
      if (err instanceof WhatIfCooldownError) setSeconds(err.seconds);
      else if (err instanceof ApiError && err.status === 422) {
        setErrorKey(`jade.whatIf.errors.${err.message === "invalid_amount" || !/^[a-z_]+$/.test(err.message) ? "invalidAmount" : err.message === "empty_version" ? "emptyVersion" : "stale"}`);
      } else setErrorKey("jade.replay.loadError");
    }
    finally { setPending(false); }
  }
  if (hadLoadError || !me) return <p role="alert">{profile("errorLoadFailed")}</p>;
  if (!isJade) return <Card><CardContent className="space-y-3 pt-6"><p>{t("access")}</p><Link className="underline" href={href("/jade")}>{t("jadeLink")}</Link></CardContent></Card>;
  return <div className="flex min-w-0 flex-col gap-6">
    <Card><CardHeader><h1 className="font-heading text-2xl">{t("title")}</h1></CardHeader><CardContent className="space-y-3 text-sm"><p>{t("intro")}</p><Link className="underline" href={href("/guides/jade-what-if")}>{t("guideLink")}</Link></CardContent></Card>
    <div className="flex flex-wrap gap-3"><CurrencySwitcher value={currency} disabled={pending || loading} onChange={value => { if (value === currency) return; setLoading(true); setErrorKey(null); setCurrent([]); setVersions([]); setCurrency(value); }} /><p className="text-sm text-muted-foreground">{t("currencyHint")}</p></div>
    {loading && <p role="status">{common("calculating")}</p>}
    {!loading && base === BigInt(0) && !errorKey && <p>{t("noHoldings")} <Link className="underline" href={href("/holdings")}>{t("holdingsLink")}</Link></p>}
    {!loading && base > BigInt(0) && <>
      <Card className="min-w-0"><CardContent className="flex min-w-0 flex-col gap-4 pt-6">
        <div className="overflow-x-auto">
          <div className="grid min-w-[48rem] grid-cols-[minmax(9rem,1fr)_repeat(3,minmax(12rem,1fr))] gap-3 text-sm">
            <div />
            {names.map((name, column) => <div key={name} className="space-y-2 font-medium">
              <p>{name}</p>
              {column > 0 && <label className="flex flex-col gap-2 font-normal">{t("rule")}<select aria-label={t("rule")} className="rounded-md border bg-background p-2" disabled={pending} value={versions[column - 1].rule} onChange={event => edit(column - 1, { ...versions[column - 1], rule: event.target.value as RebalanceRule })}>{(["none", "monthly", "quarterly"] as const).map(rule => <option key={rule} value={rule}>{t(`rules.${rule}`)}</option>)}</select></label>}
            </div>)}
            {editorRows.map(editorRow => <div key={editorRow.id} data-testid={`row-${editorRow.id}`} className="contents">
              <div className="break-words py-3">{editorRow.name}</div>
              <div className="space-y-2 rounded-md border p-3">{current.some(row => row.id === editorRow.id) ? <><p>{money(cents(editorRow.amount))}</p><p>{pct(tenths(cents(editorRow.amount), base))}</p></> : "—"}</div>
              {versions.map((version, index) => {
                const row = version.rows.find(row => row.id === editorRow.id);
                if (!row) return <div key={index} className="rounded-md border p-3">—</div>;
                const invalid = normalizeAmount(row.amount) === null;
                const changed = cellChanged(row, currentAmount(row));
                const changeAmount = (amount: string) => edit(index, { ...version, rows: version.rows.map(value => value.id === row.id ? { ...value, amount } : value) });
                return <div key={index} data-testid={`editor-cell-${row.id}-${index === 0 ? "a" : "b"}`} className={`space-y-2 rounded-md p-3 ${invalid ? "border-2 border-destructive" : changed ? "border-2 border-foreground" : "border"}`}>
                  <input aria-label={row.name} type="text" inputMode="decimal" aria-invalid={invalid ? true : undefined} className={`w-full rounded border bg-background p-2${invalid ? " border-destructive" : ""}`} value={row.amount} disabled={pending} onBlur={() => { const normalized = normalizeAmount(row.amount); if (normalized !== null) changeAmount(normalized); }} onChange={event => changeAmount(event.target.value)} />
                  <p>{pct(tenths(cents(normalizeAmount(row.amount) ?? "0"), total(version.rows)))}</p>
                  {row.kind !== "holding" && <Button variant="outline" size="sm" disabled={pending} onClick={() => edit(index, { ...version, rows: version.rows.filter(value => value.id !== row.id) })}>{t("remove")}</Button>}
                </div>;
              })}
            </div>)}
            <div /><div />
            {versions.map((version, index) => <label key={index} className="flex flex-col gap-2">{t("add")}<select aria-label={t("add")} className="max-w-full rounded border bg-background p-2" disabled={pending} value="" onChange={event => {
              const symbol = event.target.value; if (!symbol) return;
              const row: Row = symbol === "cash" ? { id: "cash", kind: "cash", name: t("cash"), amount: "0" } : { id: `etf-${symbol}`, kind: "etf", symbol, name: symbol, amount: "0" };
              setAddedOrder(order => order.includes(row.id) ? order : [...order, row.id]);
              edit(index, { ...version, rows: [...version.rows, row] });
            }}><option value="">{t("add")}</option>{symbols.filter(symbol => !version.rows.some(row => row.kind === "etf" && row.symbol === symbol)).map(symbol => <option key={symbol} value={symbol}>{symbol}</option>)}{!version.rows.some(row => row.kind === "cash") && <option value="cash">{t("cash")}</option>}</select></label>)}
            <div />
            {[current, ...versions.map(version => version.rows)].map((rows, index) => {
              const key = ["current", "a", "b"][index], sum = total(rows);
              return <dl key={key} className="grid grid-cols-[1fr_auto] gap-2"><dt>{t("total")}</dt><dd data-testid={`editor-total-${key}`}>{money(sum)}</dd><dt>{t("ratio")}</dt><dd data-testid={`editor-ratio-${key}`}>{pct(tenths(sum, base))}</dd><dt>{t("difference")}</dt><dd data-testid={`editor-difference-${key}`}>{money(sum - base, true)}</dd></dl>;
            })}
          </div>
        </div>
        {excluded.length > 0 && <div className="text-sm"><p>{t("notIncluded")}</p><ul>{excluded.map(row => <li key={row.holding_id}>{row.name}</li>)}</ul></div>}
      </CardContent></Card>
      {invalidAmount && <p role="alert" className="text-destructive">{t("errors.invalidAmount")}</p>}
      <details data-testid="etf-list" className="min-w-0 text-sm"><summary className="cursor-pointer">{t("etfList")}</summary><ul className="mt-3 space-y-2">{symbols.map(symbol => <li key={symbol} data-symbol={symbol}>{symbol} — {t(`etfs.${symbol.replaceAll(".", "_")}`)}</li>)}</ul></details>
      <ReplayRangeTabs value={range} onChange={setRange} disabled={pending} />
      <Button className="self-start" disabled={pending || seconds > 0 || loading || invalidAmount || !modified.some(Boolean)} onClick={() => outside.length ? setConfirm(true) : void submit()}>{t("calculate")}</Button>
    </>}
    {seconds > 0 && <p role="status" data-testid="cooldown">{t("cooldown")} {t("countdown", { seconds })}</p>}
    {errorKey && <p role="alert" className="text-destructive">{copy(errorKey)}</p>}
    <div className="space-y-2 text-sm text-muted-foreground"><p>{t("hindsight")}</p><p>{t("replayAnchoring")}</p><p>{t("stressAnchoring")}</p><p>{t("notModelled")}</p><p>{t("watermark")}</p></div>
    <CalculatingOverlay slow slowNotice={common("calculatingSlow")} active={pending} label={common("calculating")}><div className="min-h-24 min-w-0">{data && <WhatIfResults data={data} />}</div></CalculatingOverlay>
    <AlertDialog open={confirm} onOpenChange={setConfirm}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t("confirmTitle")}</AlertDialogTitle><AlertDialogDescription>{t("confirmRatios", { versions: outside.map(v => `${names[v.i + 1]}: ${pct(v.ratio)}`).join(t("listSeparator")) })}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>{t("cancel")}</AlertDialogCancel><AlertDialogAction onClick={() => void submit()}>{t("confirm")}</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </div>;
}
