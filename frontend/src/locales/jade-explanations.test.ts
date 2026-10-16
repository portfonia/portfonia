import { expect, it } from "vitest";
import { catalogs } from "./index";

const PATHS = [
  "jade.replay.whatItSays",
  "jade.replay.rangeHelp",
  "jade.tailRisk.whatItSays",
  "jade.tailRisk.readChart",
  "jade.stress.whatItSays",
  "jade.stress.readBars",
  "jade.style.whatItSays",
  "jade.style.readBars",
  "jade.tracking.whatItSays",
  "jade.tailRisk.intro",
  "jade.replay.methods.fund_nav",
  "jade.replay.sources",
  "jade.replay.method",
];

it.each(["en", "zh-Hans", "zh-Hant"] as const)("733 A8 %s Jade explanations stay non-advisory", locale => {
  for (const key of PATHS) {
    const actual = key.split(".").reduce<unknown>((node, part) => (node as Record<string, unknown>)?.[part], catalogs[locale]);
    expect(actual).not.toMatch(/\b(should|recommend\w*|buy|sell)\b|\u5efa\u8bae|\u5efa\u8b70|\u4e70\u5165|\u8cb7\u5165|\u5356\u51fa|\u8ce3\u51fa/i);
  }
  expect(catalogs[locale].jade.stress).not.toHaveProperty("contributionsNote");
});

it("733 review Chinese Jade strings have no space after a full stop", () => {
  function offendingPaths(value: unknown, path: string): string[] {
    if (typeof value === "string") return value.includes("\u3002 ") ? [path] : [];
    if (value && typeof value === "object") {
      return Object.entries(value).flatMap(([key, child]) => offendingPaths(child, `${path}.${key}`));
    }
    return [];
  }
  expect(["zh-Hans", "zh-Hant"].flatMap(locale => offendingPaths(catalogs[locale as "zh-Hans" | "zh-Hant"].jade, `${locale}.jade`))).toEqual([]);
});
