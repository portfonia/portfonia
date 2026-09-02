import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { REPORT_LANGUAGES } from "./index";

// backend/app/models/user.py's VALID_REPORT_LANGUAGES (issue #308) is the
// backend authority for which report-language codes are legal — it backs
// both the users.locale CheckConstraint and every Pydantic Literal that
// must be kept in sync with it by hand (PATCH /me/report-language's
// UpdateReportLanguageBody, the Ops by-email sibling). Those two Python
// Literals already have a same-language drift-guard test
// (test_me_report_language.py / test_admin_report_language.py comparing
// get_args() against the tuple); this file is the missing CROSS-language
// half — REPORT_LANGUAGES (frontend/src/locales/index.ts) had never been
// checked against the backend at all before this test (blacktomb42 PR #309
// review, finding 3).
//
// Same discipline as glossary-consistency.test.ts (issue #209): there is
// no shared source file across the Python/TypeScript boundary, so this
// test reads the actual backend source at test time and extracts the
// tuple literal, rather than hand-copying a second `["en", "zh"]` constant
// here that could silently drift out from under both of them.
// process.cwd()-relative rather than import.meta.url-relative: under
// vitest's jsdom test environment, import.meta.url resolves to a
// synthetic (non-file://) URL, not this file's real path. Vitest always
// runs from the frontend/ package root regardless of which directory
// `bun run test` was invoked from.
const USER_MODEL_PATH = path.join(process.cwd(), "../backend/app/models/user.py");

function backendValidReportLanguages(): string[] {
  const source = readFileSync(USER_MODEL_PATH, "utf8");
  const match = source.match(/VALID_REPORT_LANGUAGES\s*=\s*\(([^)]*)\)/);
  if (!match) {
    throw new Error(
      `VALID_REPORT_LANGUAGES not found in ${USER_MODEL_PATH} — has it been renamed or moved?`,
    );
  }
  const values = Array.from(match[1].matchAll(/"([^"]+)"/g)).map((m) => m[1]);
  if (values.length === 0) {
    throw new Error(`VALID_REPORT_LANGUAGES matched but no quoted values were parsed out of it`);
  }
  return values;
}

describe("REPORT_LANGUAGES vs backend VALID_REPORT_LANGUAGES (issue #308)", () => {
  it("carries exactly the same values as the backend whitelist — no more, no fewer", () => {
    const backendValues = backendValidReportLanguages();
    const frontendValues = REPORT_LANGUAGES.map((l) => l.value);
    expect(new Set(frontendValues)).toEqual(new Set(backendValues));
  });
});
