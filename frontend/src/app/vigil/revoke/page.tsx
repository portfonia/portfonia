import { PublicActionShell } from "../_components/public-action-shell";

// Public shell (issue #453) — see confirm/page.tsx's comment; same
// exemptions, same scaffolding-only scope. Real revoke logic ships
// with #462.
export default function VigilRevokePage() {
  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <PublicActionShell action="revoke" />
    </main>
  );
}
