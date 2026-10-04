"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { listReports, type ReportListPage } from "@/lib/api";

export function ReportsList({ initialPage, initialLoadError = false }: { initialPage: ReportListPage | null; initialLoadError?: boolean }) {
  const t = useTranslations("reports");
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const queryString = searchParams.toString();
  const [data, setData] = useState(initialPage);
  const [error, setError] = useState(initialLoadError);
  const params = new URLSearchParams(queryString);
  const page = params.get("page") ?? "1";
  const sort = params.get("sort") ?? "desc";
  const kind = params.get("kind") ?? "report";
  const dateFrom = params.get("date_from") ?? "";
  const dateTo = params.get("date_to") ?? "";

  useEffect(() => {
    let cancelled = false;
    const urlParams = new URLSearchParams(queryString);
    const query: Record<string, string> = { page: urlParams.get("page") ?? "1", sort: urlParams.get("sort") ?? "desc", kind: urlParams.get("kind") ?? "report" };
    for (const key of ["date_from", "date_to"]) {
      const value = urlParams.get(key);
      if (value) query[key] = value;
    }
    const request = !queryString && initialPage ? Promise.resolve(initialPage) : listReports(query);
    void request.then(result => {
      if (!cancelled) { setData(result); setError(false); }
    }).catch(() => { if (!cancelled) setError(true); });
    return () => { cancelled = true; };
  }, [queryString, initialPage]);

  function change(key: string, value: string) {
    const next = new URLSearchParams({ page, sort, kind });
    if (dateFrom) next.set("date_from", dateFrom);
    if (dateTo) next.set("date_to", dateTo);
    if (value) next.set(key, value); else next.delete(key);
    if (key !== "page") next.set("page", "1");
    router.push(`${pathname}?${next.toString()}`, { scroll: false });
  }
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  const inputClass = "rounded-md border border-border bg-background p-2 text-sm";
  const buttonClass = "rounded-md border border-border px-3 py-2 text-sm disabled:opacity-40";
  return <>
    <h1 className="mb-6 font-serif text-3xl">{t("title")}</h1>
    <div className="mb-6 flex flex-wrap gap-4">
      <label className="flex flex-col gap-1">{t("category")}<select className={inputClass} value={kind} onChange={e => change("kind", e.target.value)}>{(data?.kinds ?? ["report"]).map(value => <option key={value} value={value}>{t("title")}</option>)}</select></label>
      <label className="flex flex-col gap-1">{t("sort")}<select className={inputClass} value={sort} onChange={e => change("sort", e.target.value)}><option value="desc">{t("newest")}</option><option value="asc">{t("oldest")}</option></select></label>
      <label className="flex min-w-0 flex-col gap-1">{t("dateFrom")}<input type="date" className={inputClass} value={dateFrom} onChange={e => change("date_from", e.target.value)} /></label>
      <label className="flex min-w-0 flex-col gap-1">{t("dateTo")}<input type="date" className={inputClass} value={dateTo} onChange={e => change("date_to", e.target.value)} /></label>
    </div>
    {error ? <p role="alert">{t("loadError")}</p> : data && <>
      {data.total === 0 ? <p>{t(dateFrom || dateTo ? "emptyFiltered" : "empty")}</p> : <ul className="divide-y divide-border rounded-lg border border-border">
        {data.items.map(item => {
          const type = item.session_node === "daily_close" ? "daily" : item.session_node === "after_close" ? "everyOtherDay" : item.session_node === "weekend_snapshot" ? "weekly" : item.session_node === "manual" ? "manual" : "report";
          const content = <><span>{item.report_date}</span><span>{t(`types.${type}`)}</span>{item.display_state !== "available" && <span className="rounded-full border border-border px-2 py-1 text-xs">{t(item.display_state === "under_review" ? "underReview" : "generating")}</span>}</>;
          return <li key={item.id} data-testid="report-row">{item.display_state === "available" ? <Link href={`/reports/${item.id}`} className="flex flex-wrap items-center gap-3 p-4 hover:bg-muted">{content}</Link> : <div className="flex flex-wrap items-center gap-3 p-4">{content}</div>}</li>;
        })}
      </ul>}
      <div className="mt-6 flex flex-wrap items-center gap-3">
        <button className={buttonClass} disabled={data.page <= 1} onClick={() => change("page", String(data.page - 1))}>{t("previous")}</button>
        <span>{t("page", { page: data.page, pages })}</span>
        <button className={buttonClass} disabled={data.page >= pages} onClick={() => change("page", String(data.page + 1))}>{t("next")}</button>
      </div>
    </>}
  </>;
}
