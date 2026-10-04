import { notFound } from "next/navigation";
import { getReportServer } from "@/lib/server-api";
import { ReportDetail } from "./_components/report-detail";

export default async function ReportPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const report = await getReportServer(id);
  if (report === null) notFound();
  return <main className="report-page mx-auto w-full min-w-0 max-w-5xl px-6 py-10"><ReportDetail report={report} /></main>;
}
