import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "./locale-provider";
import { HomeSections } from "./home-sections";

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
});
