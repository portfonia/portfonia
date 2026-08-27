import { listHoldingsServer } from "@/lib/server-api";
import type { HoldingOut } from "@/lib/api";
import { HoldingsHeading } from "./_components/holdings-heading";
import { HoldingsManager } from "./_components/holdings-manager";

export default async function HoldingsPage() {
  let initialHoldings: HoldingOut[] = [];
  let initialLoadError = false;
  try {
    initialHoldings = await listHoldingsServer();
  } catch {
    initialLoadError = true;
  }

  return (
    <main className="mx-auto w-full max-w-5xl px-6 py-10">
      <HoldingsHeading />
      <HoldingsManager
        initialHoldings={initialHoldings}
        initialLoadError={initialLoadError}
      />
    </main>
  );
}
