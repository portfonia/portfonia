import { getReportsServer } from "@/lib/server-api";
import type { ReportListPage } from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { ReportsList } from "./_components/reports-list";

export default async function ReportsPage() {
  let initialPage: ReportListPage | null = null;
  let initialLoadError = false;
  try { initialPage = await getReportsServer(); }
  catch (error) { if (isNextRedirectError(error)) throw error; initialLoadError = true; }
  return <main className="mx-auto w-full min-w-0 max-w-5xl px-6 py-10"><ReportsList initialPage={initialPage} initialLoadError={initialLoadError} /></main>;
}
