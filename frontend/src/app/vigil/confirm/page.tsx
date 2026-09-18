import { PublicActionShell } from "../_components/public-action-shell";

// Public shell (issue #453/#458) — exempted from proxy.ts's Supabase lookup
// and from SiteHeader's GetStartedMenu (see EXACT_PUBLIC_VIGIL_PAGES /
// PUBLIC_VIGIL_SHELL_ROUTES). Drill confirmation is an explicit click; the
// page mount itself does not POST confirm or consume a nonce.
export default function VigilConfirmPage() {
  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <PublicActionShell action="confirm" />
    </main>
  );
}
