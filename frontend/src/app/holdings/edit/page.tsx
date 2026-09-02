import { listHoldingsServer, getMeServer } from "@/lib/server-api";
import type { HoldingOut } from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { HoldingsEditor } from "../_components/holdings-editor";

export default async function HoldingsEditPage() {
  let initialHoldings: HoldingOut[] = [];
  let initialLoadError = false;
  let onboardingIncomplete = false;
  try {
    initialHoldings = await listHoldingsServer();
  } catch (err) {
    if (isNextRedirectError(err)) throw err;
    initialLoadError = true;
  }
  try {
    const me = await getMeServer();
    onboardingIncomplete = me.missing.includes("holdings");
  } catch (err) {
    if (isNextRedirectError(err)) throw err;
  }

  return (
    <main className="mx-auto w-full max-w-5xl px-6 py-10">
      <HoldingsEditor
        initialHoldings={initialHoldings}
        initialLoadError={initialLoadError}
        onboardingIncomplete={onboardingIncomplete}
      />
    </main>
  );
}
