// @vitest-environment node
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const root = join(dirname(fileURLToPath(import.meta.url)), "../..");

describe("host-only cookies", () => {
  it.each([
    "src/lib/supabase/browser.ts",
    "src/lib/supabase/server.ts",
    "src/proxy.ts",
    "src/app/_components/login-handoff.tsx",
  ])("%s never sets Domain=.portfonia.com", (rel) => {
    const text = readFileSync(join(root, rel), "utf8");
    expect(text).not.toMatch(/domain\s*[:=]\s*['"]\.portfonia\.com['"]/i);
    expect(text).not.toContain("Domain=.portfonia.com");
    expect(text).not.toMatch(/localStorage\.setItem/);
  });
});
