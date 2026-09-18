import { VigilSetupPageBody } from "./vigil-setup-page-body";

// Protected by the default proxy.ts gate — deliberately NOT in
// EXACT_PUBLIC_VIGIL_PAGES or PUBLIC_PATH_PREFIXES (issue #453's Contract
// constraint: "do NOT exempt /vigil/setup"). The configuration/upload
// pipeline lives in VigilSetupPageBody + _lib/use-vigil-setup.ts (#455);
// this file itself stays a thin route wrapper so the auth boundary is
// independent of whatever that body renders.
export default function VigilSetupPage() {
  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <VigilSetupPageBody />
    </main>
  );
}
