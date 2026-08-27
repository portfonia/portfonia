import { describe, expect, it } from "vitest";

import { catalogs, LOCALES } from "./index";

// Arrays hold raw sample/structured data (home.preview.holdingsRows etc.),
// not per-key translated strings — comparing their contents across locales
// isn't meaningful (row count/order legitimately differs from column
// headers), so an array is treated as one leaf at its own path rather than
// recursed into.
function leafPaths(value: unknown, prefix = ""): string[] {
  if (Array.isArray(value)) return [prefix];
  if (value !== null && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).flatMap(([key, v]) =>
      leafPaths(v, prefix ? `${prefix}.${key}` : key),
    );
  }
  return [prefix];
}

const LOCALE_VALUES = LOCALES.map((l) => l.value);

describe("locale catalogs stay structurally in sync (issue #209)", () => {
  const shapes = Object.fromEntries(
    LOCALE_VALUES.map((locale) => [locale, new Set(leafPaths(catalogs[locale]))]),
  );

  it.each(LOCALE_VALUES)(
    "%s has no key paths missing from any other locale (adding a 4th locale must not silently drop content)",
    (locale) => {
      for (const other of LOCALE_VALUES) {
        if (other === locale) continue;
        const missingFromOther = [...shapes[locale]].filter((p) => !shapes[other].has(p));
        expect(missingFromOther, `${locale} has keys missing from ${other}`).toEqual([]);
      }
    },
  );

  it("every locale carries exactly the expected top-level namespaces", () => {
    const expected = ["auth", "common", "holdings", "home", "menu", "questionnaire"].sort();
    for (const locale of LOCALE_VALUES) {
      expect(Object.keys(catalogs[locale]).sort()).toEqual(expected);
    }
  });
});
