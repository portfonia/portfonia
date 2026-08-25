import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { homeMessages, type Locale } from "@/lib/i18n/home-messages";
import { LocaleProvider } from "./locale-provider";
import { HomeSections } from "./home-sections";

function parseUsdAmount(cell: string): number {
  return Number(cell.replace(/\*\*/g, "").replace(/,/g, ""));
}

function parsePercent(cell: string): number {
  return Number(cell.replace(/\*\*/g, "").replace("%", ""));
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

  it.each(["en", "zh"] as Locale[])(
    "keeps %s sample snapshot internally consistent (USD subtotals = total, weights = 100)",
    (locale) => {
      const preview = homeMessages[locale].preview;
      const totalMatch = preview.totalLine.match(/[\d,]+/);
      expect(totalMatch).not.toBeNull();
      const advertisedTotal = Number(totalMatch![0].replace(/,/g, ""));

      const usdSubtotals = preview.holdingsRows.filter(
        (row) => row[0].includes("subtotal") || row[0].includes("小计"),
      );
      const subtotalSum = usdSubtotals.reduce(
        (sum, row) => sum + parseUsdAmount(row[2]),
        0,
      );
      const eRow = preview.holdingsRows.find(
        (row) => row[4] === "Custodian E" || row[4] === "机构 E",
      );
      expect(eRow).toBeDefined();
      expect(subtotalSum + parseUsdAmount(eRow![2])).toBe(advertisedTotal);

      const weightSum =
        usdSubtotals.reduce((sum, row) => sum + parsePercent(row[3]), 0) +
        parsePercent(eRow![3]);
      expect(weightSum).toBeCloseTo(100, 5);

      const cnhUsd = parseUsdAmount(
        usdSubtotals.find((row) => row[0].includes("Custodian D") || row[0].includes("机构 D"))![2],
      );
      const cnhFace = preview.holdingsRows
        .filter((row) => row[1] === "CNH" || row[1] === "人民币")
        .reduce((sum, row) => sum + parseUsdAmount(row[2]), 0);
      expect(cnhFace / 7.15).toBeCloseTo(cnhUsd, 0);

      const hkdUsd = parseUsdAmount(
        usdSubtotals.find((row) => row[0].includes("Custodian C") || row[0].includes("机构 C"))![2],
      );
      const hkdFace = preview.holdingsRows
        .filter((row) => row[1] === "HKD" || row[1] === "港元")
        .reduce((sum, row) => sum + parseUsdAmount(row[2]), 0);
      expect(hkdFace / 7.8).toBeCloseTo(hkdUsd, 0);
    },
  );
});
