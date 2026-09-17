import { describe, expect, it } from "vitest";

import { deriveVigilDisplayState } from "./vault-display-state";
import type { VigilVaultLoadResult } from "@/lib/vigil/server";
import type { VigilVaultStatus } from "@/lib/vigil/api";

function ok(overrides: Partial<VigilVaultStatus>): VigilVaultLoadResult {
  return {
    status: "ok",
    vault: {
      vault_id: null,
      phase: "DISARMED",
      revision: 0,
      hold_reason: null,
      deadline_at: null,
      ...overrides,
    },
  };
}

describe("deriveVigilDisplayState", () => {
  it("returns unavailable for a non-ok load (403/503/network error alike)", () => {
    expect(deriveVigilDisplayState({ status: "unavailable" })).toBe("unavailable");
    expect(deriveVigilDisplayState({ status: "error" })).toBe("unavailable");
  });

  // Matches the actual #504 backend behavior: no vigil_vaults row for this
  // owner yet -> vault_id null, phase DISARMED, revision 0. This is the
  // ONLY real, exercisable case today.
  it("returns setupRequired when vault_id is null, regardless of phase", () => {
    const result = ok({ vault_id: null, phase: "DISARMED", revision: 0 });
    expect(deriveVigilDisplayState(result)).toBe("setupRequired");
  });

  it("returns hold whenever hold_reason is set, overriding the phase", () => {
    const result = ok({
      vault_id: "11111111-1111-1111-1111-111111111111",
      phase: "ARMED",
      hold_reason: "dependency_unavailable",
    });
    expect(deriveVigilDisplayState(result)).toBe("hold");
  });

  it("returns disarmed for a configured vault currently DISARMED", () => {
    const result = ok({ vault_id: "11111111-1111-1111-1111-111111111111", phase: "DISARMED" });
    expect(deriveVigilDisplayState(result)).toBe("disarmed");
  });

  it("returns active for phase ARMED with no hold", () => {
    const result = ok({ vault_id: "11111111-1111-1111-1111-111111111111", phase: "ARMED" });
    expect(deriveVigilDisplayState(result)).toBe("active");
  });

  it.each(["CHALLENGE_1", "CHALLENGE_2", "FINAL_WARNING"])(
    "returns waitingDelivery for %s with no deadline_at set yet",
    (phase) => {
      const result = ok({
        vault_id: "11111111-1111-1111-1111-111111111111",
        phase,
        deadline_at: null,
      });
      expect(deriveVigilDisplayState(result)).toBe("waitingDelivery");
    },
  );

  it.each(["CHALLENGE_1", "CHALLENGE_2", "FINAL_WARNING"])(
    "returns countdown for %s once deadline_at is set (delivery evidence anchored the grace window)",
    (phase) => {
      const result = ok({
        vault_id: "11111111-1111-1111-1111-111111111111",
        phase,
        deadline_at: "2026-09-20T00:00:00Z",
      });
      expect(deriveVigilDisplayState(result)).toBe("countdown");
    },
  );

  it("returns released for phase RELEASED", () => {
    const result = ok({ vault_id: "11111111-1111-1111-1111-111111111111", phase: "RELEASED" });
    expect(deriveVigilDisplayState(result)).toBe("released");
  });

  it("returns revoked for phase REVOKED", () => {
    const result = ok({ vault_id: "11111111-1111-1111-1111-111111111111", phase: "REVOKED" });
    expect(deriveVigilDisplayState(result)).toBe("revoked");
  });

  it("falls back to unavailable for any unrecognized phase (forward-compat, never crashes the page)", () => {
    const result = ok({ vault_id: "11111111-1111-1111-1111-111111111111", phase: "SOMETHING_NEW" });
    expect(deriveVigilDisplayState(result)).toBe("unavailable");
  });
});
