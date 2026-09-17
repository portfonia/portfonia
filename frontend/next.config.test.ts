import { describe, expect, it } from "vitest";

import nextConfig from "./next.config";

// blacktomb42 review, PR #506: Design section 5 ("Apply no-store/
// no-referrer and self-hosted resource policy" on the three public Vigil
// shells) wasn't wired up as actual response headers. No secrets are
// rendered yet (scaffolding-only per #453), but this closes the gap now
// rather than leaving it for whoever adds the real POST actions in #460+
// to remember on their own.
describe("next.config.ts headers()", () => {
  it.each(["/vigil/confirm", "/vigil/retrieve", "/vigil/revoke"])(
    "sets Cache-Control: no-store and Referrer-Policy: no-referrer on %s",
    async (path) => {
      const rules = await nextConfig.headers!();
      const matching = rules.filter((rule) => rule.source === path);

      expect(matching.length).toBeGreaterThan(0);
      for (const rule of matching) {
        const byKey = Object.fromEntries(rule.headers.map((h) => [h.key, h.value]));
        expect(byKey["Cache-Control"]).toBe("no-store");
        expect(byKey["Referrer-Policy"]).toBe("no-referrer");
      }
    },
  );

  it("does not apply the no-store/no-referrer headers to unrelated product pages", async () => {
    const rules = await nextConfig.headers!();
    const affectsHoldings = rules.some((rule) => rule.source === "/holdings");

    expect(affectsHoldings).toBe(false);
  });
});
