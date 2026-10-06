"use client";
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import type { ReportDetail as ReportDetailData } from "@/lib/api";
import { DownloadConfirmDialog, type PendingDownload } from "@/components/download-confirm-dialog";

export function ReportDetail({ report }: { report: ReportDetailData }) {
  const t = useTranslations("reports");
  const body = useRef<HTMLDivElement>(null);
  const [pending, setPending] = useState<PendingDownload | null>(null);
  useEffect(() => {
    body.current?.querySelectorAll("table").forEach(table => {
      if (table.parentElement?.classList.contains("report-table-scroll")) return;
      const wrapper = document.createElement("div");
      wrapper.className = "report-table-scroll";
      table.before(wrapper); wrapper.appendChild(table);
    });
  }, [report.report_body_html]);
  function download() {
    setPending({
      blob: new Blob([report.report_md], { type: "text/markdown;charset=utf-8" }),
      filename: `portfonia-briefing-${report.report_date}.md`,
      description: t("downloadDescription", { date: report.report_date }),
    });
  }
  return <>
    <div data-print="hide" className="mb-6 flex flex-wrap gap-3">
      <button className="rounded-md border border-border px-3 py-2" onClick={download}>{t("downloadMd")}</button>
      <button className="rounded-md border border-border px-3 py-2" onClick={() => window.print()}>{t("print")}</button>
    </div>
    <div ref={body} className="report-body" dangerouslySetInnerHTML={{ __html: report.report_body_html }} />
    <DownloadConfirmDialog pending={pending} onClose={() => setPending(null)} />
  </>;
}
