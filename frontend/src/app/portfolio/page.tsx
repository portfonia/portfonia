import { getPortfolioSummaryServer } from "@/lib/server-api";
import type { PortfolioSummary } from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { PortfolioPageBody } from "./_components/portfolio-page-body";

export default async function PortfolioPage() {
  let initialSummary: PortfolioSummary | null = null;
  let initialLoadError = false;
  try {
    // Issue #350 item 1: no currency argument — the backend resolves the
    // caller's own persisted report-currency preference when base_currency
    // is omitted, so the initial page load reflects that preference rather
    // than always seeding a hardcoded default.
    initialSummary = await getPortfolioSummaryServer();
  } catch (err) {
    // A 401 here can be the idle-logout Server Action's own redirect() throw
    // (issue #235/#240), same reasoning as the holdings page — it must
    // propagate, not be swallowed as a load error.
    if (isNextRedirectError(err)) throw err;
    initialLoadError = true;
  }

  return (
    <main className="mx-auto w-full max-w-5xl px-6 py-10">
      <PortfolioPageBody initialSummary={initialSummary} initialLoadError={initialLoadError} />
    </main>
  );
}
