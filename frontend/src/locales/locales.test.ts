import { describe, expect, it } from "vitest";

import { catalogs, type Locale } from "./index";

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

// Every catalog-backed locale, not just LOCALES (the switcher-exposed
// subset) — this test must keep covering a locale pending human review
// (e.g. zh-Hant) so its shape can't silently drift while it's excluded from
// the switcher (blacktomb42 review, PR #226).
const LOCALE_VALUES = Object.keys(catalogs) as Locale[];

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
    const expected = [
      "auth",
      "common",
      "emailVerification",
      "holdings",
      "home",
      "legal",
      "menu",
      "portfolio",
      "profile",
      "questionnaire",
      "unsubscribe",
      "welcome",
    ].sort();
    for (const locale of LOCALE_VALUES) {
      expect(Object.keys(catalogs[locale]).sort()).toEqual(expected);
    }
  });
});

describe("legal copy for Paddle review", () => {
  it.each(LOCALE_VALUES)("%s contains no superseded names, placeholders, or status copy", (locale) => {
    expect(JSON.stringify(catalogs[locale])).not.toMatch(/Portfonia LLC|Portfonia AI|\{\{|closed beta|legal review/i);
  });

  it.each(LOCALE_VALUES)("%s names the merchant of record only via placeholders and the reseller notice", (locale) => {
    const { resellerNotice, ...documents } = catalogs[locale].legal;
    const serialized = JSON.stringify(documents);
    expect(serialized).not.toMatch(/Paddle/);
    expect(serialized).toContain("{merchantOfRecord}");
    expect(serialized).toContain("{resellerNotice}");
    expect(resellerNotice).toMatch(/Paddle/);
  });

  it("keeps Paddle's required English reseller notice verbatim", () => {
    expect(catalogs.en.legal.resellerNotice).toBe(
      "Our order process is conducted by our online reseller Paddle.com. Paddle.com is the Merchant of Record for all our orders. Paddle provides all customer service inquiries and handles returns.",
    );
  });

  it.each(LOCALE_VALUES)("%s Privacy retention does not promise deletion of all associated data", (locale) => {
    expect(catalogs[locale].legal.privacy.sections[5].body[0]).not.toMatch(/full deletion|彻底删除|徹底刪除/);
  });

  it("keeps the required Terms and Privacy section structure", () => {
    expect(catalogs.en.legal.terms.sections).toHaveLength(13);
    expect(catalogs.en.legal.terms.sections[3].heading).toBe("4. Payments and Credits");
    expect(catalogs.en.legal.privacy.sections).toHaveLength(12);
  });
});
