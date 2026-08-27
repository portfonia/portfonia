import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { catalogs, type Locale } from "@/locales";
import { LocaleProvider } from "./locale-provider";
import { HomeSections } from "./home-sections";

const LOCALES: Locale[] = ["en", "zh-Hans", "zh-Hant"];

function parseUsdAmount(cell: string): number {
  return Number(cell.replace(/\*\*/g, "").replace(/,/g, ""));
}

function parsePercent(cell: string): number {
  return Number(cell.replace(/\*\*/g, "").replace("%", ""));
}

// Every locale's sample data names its institutions "<word> A".."<word> E"
// in the institution column (index 4) — e.g. "Custodian A" / "机构 A" /
// "機構 A". Deriving the localized word from holding A's own row (instead of
// hardcoding all three locales' translations here) keeps this test locale-
// agnostic.
function institutionWord(holdingsRows: string[][]): string {
  const [firstRow] = holdingsRows;
  return firstRow[4].replace(/ A$/, "");
}

function institutionRow(holdingsRows: string[][], word: string, letter: string): string[] {
  const label = `${word} ${letter}`;
  const row = holdingsRows.find((r) => r[4] === label);
  if (!row) throw new Error(`no holdingsRow with institution "${label}"`);
  return row;
}

// Subtotal rows carry the institution word + letter as a substring of the
// label column (index 0), e.g. "**Custodian D subtotal**" / "**机构 D 小计**".
function subtotalRow(subtotalRows: string[][], word: string, letter: string): string[] {
  const needle = `${word} ${letter}`;
  const row = subtotalRows.find((r) => r[0].includes(needle));
  if (!row) throw new Error(`no subtotal row containing "${needle}"`);
  return row;
}

// USD/CNH/HKD columns render as a localized currency NAME, not the ISO code,
// in the two Chinese locales (pre-existing behavior, not introduced by
// issue #209) — one set of name variants per code, checked per locale.
const CURRENCY_NAMES: Record<string, string[]> = {
  USD: ["USD", "美元"],
  CNH: ["CNH", "人民币", "人民幣"],
  HKD: ["HKD", "港元", "港幣"],
};

function isCurrency(cell: string, code: keyof typeof CURRENCY_NAMES): boolean {
  return CURRENCY_NAMES[code].includes(cell);
}

// The sample-briefing section must show EVERY report section (owner ask,
// issue #207 follow-up): snapshot, macro signals, forward calendar, holding
// analysis, risk radar — plus the subscription-tier footnote.
describe("HomeSections sample briefing", () => {
  it("renders every report section in the anonymized real-report sample", () => {
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    expect(screen.getByText("Portfolio snapshot")).toBeInTheDocument();
    expect(screen.getByText("Macro signals")).toBeInTheDocument();
    expect(screen.getByText("Forward calendar")).toBeInTheDocument();
    expect(screen.getByText("Holding analysis")).toBeInTheDocument();
    expect(screen.getByText("Risk radar")).toBeInTheDocument();
    expect(
      screen.getByText(/varies depending on your subscription tier/i),
    ).toBeInTheDocument();
  });

  it("carries the MVP closed-beta status line instead of Ring 0", () => {
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    expect(screen.getByText(/MVP — multi-user closed beta/i)).toBeInTheDocument();
    expect(screen.queryByText(/Ring 0/)).not.toBeInTheDocument();
    // The hero eyebrow renders the same MVP wording.
    expect(screen.getAllByText(/multi-user closed beta/i).length).toBeGreaterThanOrEqual(2);
  });

  it("renders bold markers in snapshot table cells as emphasis, never literal asterisks", () => {
    render(
      <LocaleProvider>
        <HomeSections />
      </LocaleProvider>,
    );

    // Subtotal rows carry ** markers in label, value AND weight columns —
    // all must render as styled text, none as raw asterisks.
    for (const text of ["358,000", "35.8%", "341,000", "34.1%"]) {
      const strongs = screen.getAllByText((_, element) => element?.tagName === "STRONG" && element.textContent === text);
      expect(strongs.length).toBeGreaterThan(0);
    }
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument();
  });

  it.each(LOCALES)(
    "keeps %s sample snapshot internally consistent (USD subtotals = total, weights = 100)",
    (locale) => {
      const preview = catalogs[locale].home.preview;
      const totalMatch = preview.totalLine.match(/[\d,]+/);
      expect(totalMatch).not.toBeNull();
      const advertisedTotal = Number(totalMatch![0].replace(/,/g, ""));

      const word = institutionWord(preview.holdingsRows);
      const usdSubtotals = preview.holdingsRows.filter(
        (row) => row[0].includes("subtotal") || row[0].includes("小计") || row[0].includes("小計"),
      );
      const subtotalSum = usdSubtotals.reduce(
        (sum, row) => sum + parseUsdAmount(row[2]),
        0,
      );
      const eRow = institutionRow(preview.holdingsRows, word, "E");
      expect(subtotalSum + parseUsdAmount(eRow[2])).toBe(advertisedTotal);

      const weightSum =
        usdSubtotals.reduce((sum, row) => sum + parsePercent(row[3]), 0) +
        parsePercent(eRow[3]);
      expect(weightSum).toBeCloseTo(100, 5);

      const dSubtotalRow = subtotalRow(usdSubtotals, word, "D");
      const cnhUsd = parseUsdAmount(dSubtotalRow[2]);
      const cnhFace = preview.holdingsRows
        .filter((row) => isCurrency(row[1], "CNH"))
        .reduce((sum, row) => sum + parseUsdAmount(row[2]), 0);
      expect(cnhFace / 7.15).toBeCloseTo(cnhUsd, 0);

      const cSubtotalRow = subtotalRow(usdSubtotals, word, "C");
      const hkdUsd = parseUsdAmount(cSubtotalRow[2]);
      const hkdFace = preview.holdingsRows
        .filter((row) => isCurrency(row[1], "HKD"))
        .reduce((sum, row) => sum + parseUsdAmount(row[2]), 0);
      expect(hkdFace / 7.8).toBeCloseTo(hkdUsd, 0);
    },
  );

  it.each(LOCALES)(
    "names only snapshot holdings in the %s risk-radar anomaly table",
    (locale) => {
      const preview = catalogs[locale].home.preview;
      const heldTickers = new Set(
        preview.holdingsRows.flatMap((row) => {
          const match = row[0].match(/\(([A-Za-z0-9.]+)\)/);
          return match ? [match[1]] : [];
        }),
      );
      for (const row of preview.anomalyRows) {
        const match = row[0].match(/\(([A-Za-z0-9.]+)\)/);
        expect(match, `anomaly ${row[0]} has no ticker`).not.toBeNull();
        expect(heldTickers.has(match![1]), `${match![1]} missing from snapshot`).toBe(true);
      }
    },
  );
});
