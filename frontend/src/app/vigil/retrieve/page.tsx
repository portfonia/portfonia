import { PublicActionShell } from "../_components/public-action-shell";

// Public shell (issue #453) — see confirm/page.tsx's comment; same
// exemptions, same scaffolding-only scope. Real retrieval logic ships
// with #461.
export default function VigilRetrievePage() {
  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <PublicActionShell action="retrieve" />
    </main>
  );
}
