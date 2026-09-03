import { describe, expect, it } from "vitest";

import { catalogs, type Locale } from "./index";

// issue #333's contract: a dim that has `optionHints` at all must cover
// every one of its `options` keys, no partial coverage; asset_scale/horizon
// must have no `optionHints` key whatsoever. Known gap named in the issue's
// design comment — no automated check existed before this test.
//
// Walks every catalog-backed locale (not just en.json, per blacktomb42's
// PR #334 review 5101780777): en/zh-Hans structurally match by construction
// today, but a future coordinated three-file miss on an excluded dim would
// only be caught here, not by en.json alone.
const EXCLUDED_DIMS = ["asset_scale", "horizon"];
const LOCALE_VALUES = Object.keys(catalogs) as Locale[];

describe("questionnaire dims.<dim>.optionHints stay in sync with dims.<dim>.options (issue #333)", () => {
  it.each(LOCALE_VALUES)("%s", (locale) => {
    const dims = catalogs[locale].questionnaire.dims as Record<
      string,
      { options: Record<string, string>; optionHints?: Record<string, string> }
    >;

    for (const dim of Object.keys(dims)) {
      const entry = dims[dim];
      if (!entry) throw new Error(`missing dim ${dim}`);
      if (EXCLUDED_DIMS.includes(dim)) {
        expect(entry.optionHints, `${locale}.${dim}`).toBeUndefined();
        continue;
      }
      expect(entry.optionHints, `${locale}.${dim}`).toBeDefined();
      const optionKeys = Object.keys(entry.options).sort();
      const hintKeys = Object.keys(entry.optionHints ?? {}).sort();
      expect(hintKeys, `${locale}.${dim}`).toEqual(optionKeys);
    }
  });
});
