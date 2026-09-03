import { describe, expect, it } from "vitest";

import en from "./en.json";

// issue #333's contract: a dim that has `optionHints` at all must cover
// every one of its `options` keys, no partial coverage; asset_scale/horizon
// must have no `optionHints` key whatsoever. Known gap named in the issue's
// design comment — no automated check existed before this test.
const EXCLUDED_DIMS = ["asset_scale", "horizon"];

describe("questionnaire dims.<dim>.optionHints stay in sync with dims.<dim>.options (issue #333)", () => {
  const dims = en.questionnaire.dims as Record<
    string,
    { options: Record<string, string>; optionHints?: Record<string, string> }
  >;

  it.each(Object.keys(dims))("%s", (dim) => {
    const entry = dims[dim];
    if (!entry) throw new Error(`missing dim ${dim}`);
    if (EXCLUDED_DIMS.includes(dim)) {
      expect(entry.optionHints).toBeUndefined();
      return;
    }
    expect(entry.optionHints).toBeDefined();
    const optionKeys = Object.keys(entry.options).sort();
    const hintKeys = Object.keys(entry.optionHints ?? {}).sort();
    expect(hintKeys).toEqual(optionKeys);
  });
});
