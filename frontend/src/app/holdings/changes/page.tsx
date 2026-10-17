"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { useLocale, useTranslations } from "next-intl";
import { ApiError, listHoldingChanges, previewHoldingRollback, rollbackHoldings, type HoldingChanges, type HoldingRollbackPreview } from "@/lib/api";
import { AlertDialog, AlertDialogContent, AlertDialogHeader, AlertDialogTitle, AlertDialogDescription, AlertDialogFooter, AlertDialogCancel, AlertDialogAction } from "@/components/ui/alert-dialog";

const FIELD_LABELS: Record<string, string> = {
  name: "fieldName", ticker: "fieldTicker", fund_code: "fieldFundCode", currency: "fieldCurrency",
  shares: "fieldShares", avg_cost: "fieldAvgCost", current_value: "fieldCurrentValue", pricing_mode: "fieldPricingMode",
  asset_type: "fieldAssetType", market: "fieldMarket", broker: "fieldBroker", account: "fieldAccount",
  portfolio: "fieldPortfolio", notes: "fieldNotes", watch_tier: "fieldWatchTier",
};

function initialDates() {
  const end = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  const first = new Date(`${end}T12:00:00Z`);
  first.setUTCDate(first.getUTCDate() - 29);
  return { start: first.toISOString().slice(0, 10), end };
}

export default function ChangesPage() {
  const t = useTranslations("holdingChanges");
  const h = useTranslations("holdings");
  const locale = useLocale();
  const [bounds] = useState(initialDates);
  const [dates, setDates] = useState(bounds);
  const [query, setQuery] = useState(bounds);
  const [data, setData] = useState<HoldingChanges | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [rollbackError, setRollbackError] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ groupId: string; value: HoldingRollbackPreview } | null>(null);
  const [posting, setPosting] = useState(false);

  useEffect(() => {
    let active = true;
    listHoldingChanges(query.start, query.end).then(result => {
      if (active) setData(result);
    }).catch(() => { if (active) setError(true); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [query]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError(false);
    setQuery({ ...dates });
  }

  async function openRollback(groupId: string) {
    setRollbackError(null);
    try {
      const value = await previewHoldingRollback(groupId);
      setPreview({ groupId, value });
    } catch (error) {
      setRollbackError(t(error instanceof ApiError && error.status === 409 && error.message === "rollback_blocked" ? "rollbackBlocked" : "rollbackError"));
    }
  }

  async function confirmRollback() {
    if (!preview) return;
    setPosting(true);
    setRollbackError(null);
    try {
      await rollbackHoldings(preview.groupId, preview.value.version);
      setPreview(null);
      setQuery({ ...query });
    } catch (error) {
      const code = error instanceof ApiError && error.status === 409 ? error.message : "";
      setRollbackError(t(code === "version_conflict" ? "versionConflict" : code === "holding_cap" ? "holdingCap" : code === "rollback_blocked" ? "rollbackBlocked" : "rollbackError"));
      setPreview(null);
      if (code === "version_conflict") setQuery({ ...query });
    } finally {
      setPosting(false);
    }
  }

  const formatTime = (value: string) => new Intl.DateTimeFormat(locale, { timeZone: "America/New_York", dateStyle: "medium", timeStyle: "long" }).format(new Date(value));

  return (
    <main className="mx-auto w-full min-w-0 max-w-5xl px-6 py-10">
      <h1 className="font-heading text-2xl font-semibold">{t("title")}</h1>
      <Link href="/holdings" className="text-sm underline">{h("backToHoldingsList")}</Link>
      <form onSubmit={submit} className="my-6 flex flex-col gap-3 sm:flex-row sm:items-end">
        {(["start", "end"] as const).map(key => (
          <label key={key} className="flex min-w-0 flex-col gap-1 text-sm">{t(key)}
            <input type="date" required min={bounds.start} max={bounds.end} value={dates[key]}
              onChange={event => setDates({ ...dates, [key]: event.target.value })} className="min-w-0 rounded border p-2" />
          </label>
        ))}
        <button type="submit" disabled={loading} className="rounded border px-4 py-2 text-sm disabled:opacity-50">{t("submit")}</button>
      </form>
      {rollbackError && <p role="alert">{rollbackError}</p>}
      {loading ? <p role="status">{t("loading")}</p> : error ? <p role="alert">{t("error")}</p> : data && (
        <div className="space-y-5">
          {data.truncated && <p role="status">{t("truncated")}</p>}
          {data.items.length === 0 && <p>{t("empty")}</p>}
          {data.items.map(group => (
            <section key={group.id} className="min-w-0 rounded border p-4 [overflow-wrap:anywhere]">
              <p className="text-sm"><time dateTime={group.changed_at}>{formatTime(group.changed_at)}</time></p>
              <p className="font-medium">{group.source === "rollback" && group.rollback_to ? t("rollback", { time: formatTime(group.rollback_to) }) : group.source === "agent" && group.token ? t("agent", group.token) : t(group.source === "file" ? "file" : "web")}</p>
              <button type="button" disabled={!group.rollback_available} onClick={() => void openRollback(group.id)} className="mt-2 rounded border px-3 py-2 text-sm disabled:opacity-50">{t("rollbackButton")}</button>
              {!group.rollback_available && <p className="mt-1 text-sm text-muted-foreground">{t("rollbackBlocked")}</p>}
              {group.rows.map(row => (
                <div key={row.holding_id} className="mt-4 min-w-0">
                  <h2 className="font-semibold">{row.holding_name}</h2>
                  <p className="text-sm text-muted-foreground">{t(row.kind)}</p>
                  {row.fields.map(field => (
                    <dl key={field.field} className="mt-2 grid min-w-0 gap-2 border-t pt-2 text-sm sm:grid-cols-3">
                      <div><dt className="text-muted-foreground">{t("field")}</dt><dd>{field.field === "position" ? t("position") : h(FIELD_LABELS[field.field])}</dd></div>
                      <div><dt className="text-muted-foreground">{t("before")}</dt><dd className="whitespace-pre-wrap">{field.before ?? "—"}</dd></div>
                      <div><dt className="text-muted-foreground">{t("after")}</dt><dd className="whitespace-pre-wrap">{field.after ?? "—"}</dd></div>
                    </dl>
                  ))}
                </div>
              ))}
            </section>
          ))}
        </div>
      )}
      <AlertDialog open={preview !== null} onOpenChange={open => { if (!open && !posting) setPreview(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("rollbackButton")}</AlertDialogTitle>
            <AlertDialogDescription>{preview && t("rollbackSummary", { groups: preview.value.groups, time: formatTime(preview.value.target_changed_at), restored: preview.value.restored, removed: preview.value.removed, modified: preview.value.modified })}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={posting}>{t("rollbackCancel")}</AlertDialogCancel>
            <AlertDialogAction disabled={posting} onClick={() => void confirmRollback()}>{t("rollbackConfirm")}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </main>
  );
}
