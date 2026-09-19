import type { VigilVaultLoadResult } from "@/lib/vigil/server";

// Pure state-derivation for the /vigil dashboard (issue #453; trimmed to
// reachable states only by #529 / #516 finding 11). GET /vigil/vault today
// only ever distinguishes "no row for this owner" (vault_id: null) from "a
// row exists" — write paths that would let a caller further distinguish
// armed/countdown/hold/released/revoked (#454+ configurations/objects/
// cycles, #456+ the outbox that sets deadline_at) haven't landed. Grow this
// enum when a write path makes one of those states real and reachable, not
// ahead of it. The payload's `phase`/`hold_reason`/`deadline_at` fields may
// still exist on `VigilVaultStatus` — this function deliberately ignores
// them for display-state purposes.
export type VigilDisplayState = "unavailable" | "setupRequired" | "disarmed";

export function deriveVigilDisplayState(result: VigilVaultLoadResult): VigilDisplayState {
  if (result.status !== "ok") return "unavailable";
  return result.vault.vault_id === null ? "setupRequired" : "disarmed";
}
