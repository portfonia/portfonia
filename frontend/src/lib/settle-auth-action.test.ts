import { beforeEach, describe, expect, it, vi } from "vitest";

const { clearPendingLogin } = vi.hoisted(() => ({
  clearPendingLogin: vi.fn(),
}));

vi.mock("@/hooks/use-session", () => ({ clearPendingLogin }));

import { settleAuthAction } from "./settle-auth-action";

describe("settleAuthAction", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("disarms the pending signal when the action returns { error }", async () => {
    const result = await settleAuthAction(
      async () => ({ error: "Invalid email or password." }),
      "Could not sign in. Try again.",
    );
    expect(result).toEqual({ error: "Invalid email or password." });
    expect(clearPendingLogin).toHaveBeenCalledTimes(1);
  });

  it("leaves the pending signal armed on a successful result with no error", async () => {
    const result = await settleAuthAction(async () => ({ error: null }), "x");
    expect(result).toEqual({ error: null });
    expect(clearPendingLogin).not.toHaveBeenCalled();
  });

  it("disarms and returns the fallback when the action throws a real error", async () => {
    const result = await settleAuthAction(async () => {
      throw new Error("auth unreachable");
    }, "Could not sign in. Try again.");
    expect(result).toEqual({ error: "Could not sign in. Try again." });
    expect(clearPendingLogin).toHaveBeenCalledTimes(1);
  });

  it("rethrown NEXT_REDIRECT does not disarm (successful login/signup redirect)", async () => {
    const redirect = { digest: "NEXT_REDIRECT;replace;/holdings;307;" };
    await expect(
      settleAuthAction(async () => {
        throw redirect;
      }, "x"),
    ).rejects.toBe(redirect);
    expect(clearPendingLogin).not.toHaveBeenCalled();
  });
});
