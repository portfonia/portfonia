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
  // owner yet -> vault_id null, phase DISARMED, revision 0.
  it("returns setupRequired when vault_id is null, regardless of phase", () => {
    const result = ok({ vault_id: null, phase: "DISARMED", revision: 0 });
    expect(deriveVigilDisplayState(result)).toBe("setupRequired");
  });

  it("returns disarmed for a configured vault currently DISARMED", () => {
    const result = ok({ vault_id: "11111111-1111-1111-1111-111111111111", phase: "DISARMED" });
    expect(deriveVigilDisplayState(result)).toBe("disarmed");
  });

  // #529 (#516 finding 11): phase/hold_reason/deadline_at are not yet
  // populated by any real write path, so display state must not branch on
  // them even though the fields exist on the payload today.
  it("returns disarmed for a vault_id-present payload regardless of phase/hold_reason/deadline_at", () => {
    const result = ok({
      vault_id: "11111111-1111-1111-1111-111111111111",
      phase: "ARMED",
      hold_reason: "dependency_unavailable",
      deadline_at: "2026-09-20T00:00:00Z",
    });
    expect(deriveVigilDisplayState(result)).toBe("disarmed");
  });
});
