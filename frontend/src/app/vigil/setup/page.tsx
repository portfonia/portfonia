import { VigilSetupPageBody } from "./vigil-setup-page-body";

// Protected by the default proxy.ts gate — deliberately NOT in
// EXACT_PUBLIC_VIGIL_PAGES or PUBLIC_PATH_PREFIXES (issue #453's Contract
// constraint: "do NOT exempt /vigil/setup"). No configuration/upload logic
// exists yet (#454/#455) — this is a protected placeholder so the route,
// its auth boundary, and the dashboard's "Set up Vigil" link all exist now.
export default function VigilSetupPage() {
  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <VigilSetupPageBody />
    </main>
  );
}
