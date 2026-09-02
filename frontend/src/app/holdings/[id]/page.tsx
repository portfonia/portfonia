import { listHoldingsServer } from "@/lib/server-api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { HoldingForm } from "../_components/holding-form";
import { HoldingMissing } from "../_components/holding-missing";

export default async function HoldingDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  let loadError = false;
  let holding = undefined;
  try {
    const holdings = await listHoldingsServer();
    holding = holdings.find((h) => h.id === id);
  } catch (err) {
    if (isNextRedirectError(err)) throw err;
    loadError = true;
  }

  if (loadError || !holding) {
    return (
      <main className="mx-auto w-full max-w-3xl px-6 py-10">
        <HoldingMissing loadError={loadError} />
      </main>
    );
  }

  return (
    <main className="mx-auto w-full max-w-3xl px-6 py-10">
      <HoldingForm initial={holding} />
    </main>
  );
}
