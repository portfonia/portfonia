// @vitest-environment node
import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";

describe("Portfonia /auth/vigil response headers", () => {
  it("sets no-store, no-referrer, no framing, and preserves opener only on the bridge path", async () => {
    const rules = await nextConfig.headers?.();
    expect(rules).toBeDefined();
    const bridge = rules!.find((rule) => rule.source === "/auth/vigil");
    expect(bridge).toBeDefined();
    const map = Object.fromEntries(bridge!.headers.map((h) => [h.key, h.value]));
    expect(map["Cache-Control"]).toBe("no-store");
    expect(map["Referrer-Policy"]).toBe("no-referrer");
    expect(map["X-Frame-Options"]).toBe("DENY");
    expect(map["Cross-Origin-Opener-Policy"]).toBe("unsafe-none");
    expect(map["Content-Security-Policy"]).toContain("frame-ancestors 'none'");
    expect(map["Content-Security-Policy"]).not.toMatch(/\*/);
  });

  it("does not apply those opener-preserving headers globally", async () => {
    const rules = await nextConfig.headers?.();
    expect(rules!.every((rule) => rule.source === "/auth/vigil")).toBe(true);
  });
});
