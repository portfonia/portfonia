import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BreakdownChart } from "./breakdown-chart";

describe("BreakdownChart", () => {
  it("shows the empty-state label when every value is zero or missing", () => {
    render(
      <BreakdownChart
        title="By market"
        data={{}}
        currency="USD"
        emptyLabel="No data yet"
      />,
    );
    expect(screen.getByText("No data yet")).toBeInTheDocument();
  });

  it("renders a legend row per non-zero slice, largest first, with amount and share", () => {
    render(
      <BreakdownChart
        title="By market"
        data={{ Other: "1000.00", US: "3000.00", Zero: "0" }}
        currency="USD"
        emptyLabel="No data yet"
      />,
    );

    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(2); // the zero-value slice is excluded
    expect(rows[0]).toHaveTextContent("US");
    expect(rows[0]).toHaveTextContent("3,000.00 USD");
    expect(rows[0]).toHaveTextContent("(75.0%)");
    expect(rows[1]).toHaveTextContent("Other");
    expect(rows[1]).toHaveTextContent("(25.0%)");
  });
});
