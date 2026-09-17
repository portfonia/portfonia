// Client-side typed access to the Vigil management API, browser side.
//
// Mirrors backend/app/schemas/vigil.py's VigilVaultStatus/VigilObjectSummary
// exactly as they exist today (#452/#504) — not the aspirational full shape
// from #450's Design section 5. `active`/`pending`/`recipients`/
// `delivery_status` are structurally present but always empty/None until
// #454+ adds the configurations/objects tables; do not add `inner`/`outer`/
// DEK fields here even speculatively — the backend never returns them and a
// client type that accepted them would be exactly the leak surface #453's
// scope explicitly forbids.

export interface VigilObjectSummary {
  id: string;
  filename: string;
  plaintext_size: number;
  status: string;
  has_password: boolean | null;
}

export interface VigilVaultStatus {
  vault_id: string | null;
  phase: string;
  revision: number;
  hold_reason?: string | null;
  next_check_at?: string | null;
  deadline_at?: string | null;
  last_scan_completed_at?: string | null;
  active?: VigilObjectSummary | null;
  pending?: VigilObjectSummary | null;
  recipients?: string[];
  delivery_status?: unknown[];
}

// Deliberately does NOT reuse lib/api.ts's throwOnHttpError: that helper
// calls the shared logout() Server Action on a 401, which is the right
// behavior for an actual page's data fetch but wrong for this endpoint's
// two current call sites (the SiteHeader nav-visibility check and the
// /vigil dashboard's own read) — see hooks/use-vigil-access.ts and
// app/vigil/page.tsx for how each one separately decides what a non-2xx
// response means (hidden nav entry vs. an "unavailable" dashboard state).
export async function getVigilVaultStatus(): Promise<VigilVaultStatus> {
  const res = await fetch("/api/vigil/vault", { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`vigil vault status request failed: ${res.status}`);
  }
  return res.json() as Promise<VigilVaultStatus>;
}
