import { listHoldingsServer } from "@/lib/server-api";
import type { HoldingOut } from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { HoldingsHeading } from "./_components/holdings-heading";
import { HoldingsManager } from "./_components/holdings-manager";

export default async function HoldingsPage({
  searchParams,
}: {
  searchParams: Promise<{ onboarding?: string }>;
}) {
  let initialHoldings: HoldingOut[] = [];
  let initialLoadError = false;
  try {
    initialHoldings = await listHoldingsServer();
  } catch (err) {
    // A 401 here can be the idle-logout Server Action's own redirect()
    // throw (issue #235/#240) — that must propagate, not be swallowed as
    // a load error.
    if (isNextRedirectError(err)) throw err;
    initialLoadError = true;
  }
  // Reached via questionnaire onboarding Skip (Ring 1-Onboarding.md §2.2) —
  // the Profile gap card links here with no query string, so it falls
  // through to "normal".
  const { onboarding } = await searchParams;
  const mode = onboarding === "1" ? "onboarding" : "normal";

  return (
    <main className="mx-auto w-full max-w-5xl px-6 py-10">
      <HoldingsHeading />
      <HoldingsManager
        initialHoldings={initialHoldings}
        initialLoadError={initialLoadError}
        mode={mode}
      />
    </main>
  );
}
