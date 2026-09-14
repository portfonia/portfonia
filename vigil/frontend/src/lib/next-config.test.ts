// @vitest-environment node
import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";

describe("vigil next.config", () => {
  it("rewrites /api to the Vigil backend, not Portfonia", async () => {
    const rules = await nextConfig.rewrites?.();
    const list = Array.isArray(rules) ? rules : [];
    expect(list).toEqual([
      {
        source: "/api/:path*",
        destination: "http://localhost:8000/:path*",
      },
    ]);
  });

  it("preserves popups on the management origin without a global COOP same-origin lock", async () => {
    const headers = await nextConfig.headers?.();
    const root = headers?.find((rule) => rule.source === "/");
    const map = Object.fromEntries((root?.headers ?? []).map((h) => [h.key, h.value]));
    expect(map["Cross-Origin-Opener-Policy"]).toBe("same-origin-allow-popups");
  });
});
