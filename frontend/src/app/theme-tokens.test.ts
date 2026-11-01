import { readFileSync } from "node:fs";
import { expect, it, vi } from "vitest";
import { GAUGE_COLORS } from "./portfolio/_components/risk-panel";
import { ASSET_CLASS_COLORS, DEFAULT_ASSET_CLASS_COLOR } from "./portfolio/performance/_components/allocation-data";

vi.mock("@/lib/api", () => ({}));

it("856 A6 defines every chart, gauge and asset token in both themes and uses token references", () => {
  const css = readFileSync("src/app/globals.css", "utf8");
  const tokens = (selector: string) => new Set([...((css.match(new RegExp(`${selector} \\{([^}]+)\\}`))?.[1] ?? "").matchAll(/(--(?:gauge|asset|chart)-[\w-]+):/g))].map(match => match[1]));
  const light = tokens(":root");
  const dark = tokens("\\.dark");
  expect([...light].sort()).toEqual([...dark].sort());
  for (const color of [...GAUGE_COLORS, ...Object.values(ASSET_CLASS_COLORS), DEFAULT_ASSET_CLASS_COLOR]) {
    expect(color).toMatch(/^var\(--(?:gauge|asset)-[\w-]+\)$/);
    expect(light.has(color.slice(4, -1))).toBe(true);
    expect(dark.has(color.slice(4, -1))).toBe(true);
  }
});
