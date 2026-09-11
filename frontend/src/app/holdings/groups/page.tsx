import { listHoldingsServer } from "@/lib/server-api";
import type { HoldingOut } from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { HoldingsGroupsEditor } from "../_components/holdings-groups-editor";

export default async function HoldingsGroupsPage() {
  let initialHoldings: HoldingOut[] = [];
  let initialLoadError = false;
  try {
    initialHoldings = await listHoldingsServer();
  } catch (err) {
    if (isNextRedirectError(err)) throw err;
    initialLoadError = true;
  }

  return (
    <main className="mx-auto w-full max-w-5xl px-6 py-10">
      <HoldingsGroupsEditor initialHoldings={initialHoldings} initialLoadError={initialLoadError} />
    </main>
  );
}
