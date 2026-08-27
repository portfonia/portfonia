import { readFileSync } from "node:fs";
import path from "node:path";
import { parse } from "yaml";
import { describe, expect, it } from "vitest";

import { catalogs } from "./index";

// backend/config/i18n_glossary.yml is the authority for terms that appear in
// both the UI and AI-generated reports (issue #209 — see src/locales/
// README.md's "Overlapping terms" section). There is no shared source file
// across the Python/YAML and TypeScript/JSON runtime boundary, so this test
// is the drift guard: it parses the YAML directly and compares it against
// this catalog's zh-Hans values for the specific overlapping terms named in
// the issue. A real instance of this drift ("托管机构" vs the glossary's
// "持仓机构" for Custodian) is exactly what got fixed alongside this test.
// process.cwd()-relative rather than import.meta.url-relative: under
// vitest's jsdom test environment, import.meta.url resolves to a synthetic
// (non-file://) URL, not this file's real path. Vitest always runs from the
// frontend/ package root regardless of which directory `bun run test` was
// invoked from.
const GLOSSARY_PATH = path.join(process.cwd(), "../backend/config/i18n_glossary.yml");

interface Glossary {
  report_glossary: Record<string, Record<string, string>>;
}

const glossary = parse(readFileSync(GLOSSARY_PATH, "utf8")) as Glossary;

describe("UI catalog vs report glossary overlapping terms (issue #209)", () => {
  it("Custodian's zh-Hans rendering matches the report glossary", () => {
    const expected = glossary.report_glossary.Custodian["zh-Hans"];
    expect(catalogs["zh-Hans"].home.preview.holdingsColumns).toContain(expected);
  });

  const TIERS = [
    { label: "Established", key: "established" as const },
    { label: "Probable", key: "probable" as const },
    { label: "Speculative", key: "speculative" as const },
  ];

  it.each(TIERS)(
    "[$label] confidence tier's zh-Hans rendering matches the report glossary",
    ({ label, key }) => {
      const bracketedExpected = glossary.report_glossary[`[${label}]`]["zh-Hans"];
      // The UI's tier chips render without brackets (home.how.tiers), to
      // match this page's bracket-free label style — strip them from the
      // glossary's bracketed form before comparing.
      const expected = bracketedExpected.replace(/^\[|\]$/g, "");
      expect(catalogs["zh-Hans"].home.how.tiers[key]).toBe(expected);
    },
  );
});
