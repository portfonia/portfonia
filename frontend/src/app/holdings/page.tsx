import { messages } from "@/lib/messages";
import { listHoldingsServer } from "@/lib/server-api";
import type { HoldingOut } from "@/lib/api";
import { HoldingsManager } from "./_components/holdings-manager";

const m = messages.holdings;

export default async function HoldingsPage() {
  let initialHoldings: HoldingOut[] = [];
  let initialError: string | null = null;
  try {
    initialHoldings = await listHoldingsServer();
  } catch {
    initialError = m.errorLoadFailed;
  }

  return (
    <main className="mx-auto w-full max-w-5xl px-6 py-10">
      <header className="mb-8">
        <h1 className="font-heading text-2xl font-semibold">{m.pageTitle}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{m.pageSubtitle}</p>
      </header>
      <HoldingsManager
        initialHoldings={initialHoldings}
        initialError={initialError}
      />
    </main>
  );
}
