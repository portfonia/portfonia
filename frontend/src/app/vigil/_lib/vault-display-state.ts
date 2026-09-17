import type { VigilVaultLoadResult } from "@/lib/vigil/server";

// Pure state-derivation for the /vigil dashboard (issue #453). Built
// honestly against what #452/#504's GET /vigil/vault ACTUALLY returns
// today — a caller with no vigil_vaults row gets {vault_id: null,
// phase: "DISARMED", revision: 0} and nothing else, so "setupRequired" is
// the only branch below reachable with real data right now. The others
// (active/waitingDelivery/countdown/hold/released/revoked) read fields the
// schema already carries (phase enum, hold_reason, deadline_at) but that no
// write path populates yet — #454+ add configurations/objects/cycles, #456+
// add the outbox that actually sets deadline_at. This is deliberate:
// rendering the full state machine now, against the real (if currently
// always-empty) response shape, means #454-#462 land as pure backend work
// with no frontend follow-up per phase.
export type VigilDisplayState =
  | "unavailable"
  | "setupRequired"
  | "disarmed"
  | "active"
  | "waitingDelivery"
  | "countdown"
  | "hold"
  | "released"
  | "revoked";

const CHALLENGE_PHASES = new Set(["CHALLENGE_1", "CHALLENGE_2", "FINAL_WARNING"]);

export function deriveVigilDisplayState(result: VigilVaultLoadResult): VigilDisplayState {
  if (result.status !== "ok") return "unavailable";

  const { vault } = result;
  if (vault.hold_reason) return "hold";
  if (vault.vault_id === null) return "setupRequired";

  switch (vault.phase) {
    case "DISARMED":
      return "disarmed";
    case "ARMED":
      return "active";
    case "RELEASED":
      return "released";
    case "REVOKED":
      return "revoked";
    default:
      if (CHALLENGE_PHASES.has(vault.phase)) {
        return vault.deadline_at ? "countdown" : "waitingDelivery";
      }
      return "unavailable";
  }
}
