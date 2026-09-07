import { getPortfolioSummaryServer } from "@/lib/server-api";
import type { PortfolioSummary } from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { PerformancePageBody } from "./_components/performance-page-body";

// Issue #360 Phase 2 — /portfolio/performance. The server component seeds
// the summary (report-currency preference + dataset filter options) exactly
// like /portfolio's page does; the performance fetch itself is client-side
// because it is driven by the interactive controls on this page.
export default async function PerformancePage() {
  let initialSummary: PortfolioSummary | null = null;
  let initialLoadError = false;
  try {
    // No base_currency argument (issue #350 item 1): the backend resolves
    // the caller's own persisted preference, so the currency switcher seeds
    // from what the user actually views elsewhere.
    initialSummary = await getPortfolioSummaryServer();
  } catch (err) {
    // Same idle-logout redirect propagation reasoning as /portfolio's page.
    if (isNextRedirectError(err)) throw err;
    initialLoadError = true;
  }

  return (
    <main className="mx-auto w-full max-w-5xl px-6 py-10">
      <PerformancePageBody initialSummary={initialSummary} initialLoadError={initialLoadError} />
    </main>
  );
}
