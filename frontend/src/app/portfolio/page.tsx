import { getPortfolioSummaryServer } from "@/lib/server-api";
import type { PortfolioSummary } from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { DEFAULT_BASE_CURRENCY } from "./_components/currencies";
import { PortfolioPageBody } from "./_components/portfolio-page-body";

export default async function PortfolioPage() {
  let initialSummary: PortfolioSummary | null = null;
  let initialLoadError = false;
  try {
    initialSummary = await getPortfolioSummaryServer(DEFAULT_BASE_CURRENCY);
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
