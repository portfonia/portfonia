import { expect, it } from "vitest";
import { catalogs } from "./index";
it("A12 calculating renderings match D4 in all three catalogs", () => {
  expect(catalogs.en.common).toHaveProperty("calculating", "Calculating…");
  expect(catalogs["zh-Hans"].common).toHaveProperty("calculating", "\u6b63\u5728\u8ba1\u7b97\u4e2d…");
  expect(catalogs["zh-Hant"].common).toHaveProperty("calculating", "\u6b63\u5728\u8a08\u7b97\u4e2d…");
});
it.each(["en", "zh-Hans", "zh-Hant"] as const)("A12 %s span keys and copy", locale => {
  const copy = catalogs[locale].jade.replay;
  expect(copy).not.toHaveProperty("loading");
  expect(Object.keys(copy.ranges)).toEqual(["1M", "3M", "6M", "YTD", "1Y", "3Y", "5Y"]);
  expect([copy.intro, copy.limitations, copy.annualizedShortNote].join(" ")).not.toMatch(/\b(should|recommend\w*|buy|sell)\b|\u5efa\u8bae|\u5efa\u8b70|\u4e70\u5165|\u8cb7\u5165|\u5356\u51fa|\u8ce3\u51fa/i);
});
