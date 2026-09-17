import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";

const { getVigilVaultStatus } = vi.hoisted(() => ({
  getVigilVaultStatus: vi.fn(),
}));

vi.mock("@/lib/vigil/api", () => ({ getVigilVaultStatus }));

import { useVigilAccess } from "./use-vigil-access";

describe("useVigilAccess", () => {
  afterEach(() => vi.resetAllMocks());

  it("starts false and never calls the backend when disabled (guest/checking session)", () => {
    const { result } = renderHook(() => useVigilAccess(false));

    expect(result.current).toBe(false);
    expect(getVigilVaultStatus).not.toHaveBeenCalled();
  });

  it("flips to true once GET /vigil/vault resolves (owner, feature active)", async () => {
    getVigilVaultStatus.mockResolvedValue({ vault_id: null, phase: "DISARMED", revision: 0 });

    const { result } = renderHook(() => useVigilAccess(true));

    await waitFor(() => expect(result.current).toBe(true));
  });

  // 403 (not the configured owner) and 503 (feature off) both reject from
  // getVigilVaultStatus — the nav entry must stay hidden, not throw and
  // break the whole header.
  it("stays false when the backend call rejects (403/503/401/network error)", async () => {
    getVigilVaultStatus.mockRejectedValue(new Error("vigil vault status request failed: 403"));

    const { result } = renderHook(() => useVigilAccess(true));

    await waitFor(() => expect(getVigilVaultStatus).toHaveBeenCalled());
    expect(result.current).toBe(false);
  });

  it("resets to false when enabled flips back off (e.g. logout)", async () => {
    getVigilVaultStatus.mockResolvedValue({ vault_id: null, phase: "DISARMED", revision: 0 });
    const { result, rerender } = renderHook(({ enabled }) => useVigilAccess(enabled), {
      initialProps: { enabled: true },
    });
    await waitFor(() => expect(result.current).toBe(true));

    rerender({ enabled: false });

    expect(result.current).toBe(false);
  });
});
