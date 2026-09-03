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

  it("uses formatValue to override the default money-in-currency amount when given", () => {
    // Issue #330: the currency card's native/percentage modes aren't a
    // single-currency money amount — formatValue lets a caller fully
    // control the displayed string per (key, value).
    render(
      <BreakdownChart
        title="By currency"
        data={{ USD: "3000.00", CNY: "1000.00" }}
        currency="USD"
        emptyLabel="No data yet"
        formatValue={(_key, value) => `${value.toFixed(1)}%`}
        showShareOfTotal={false}
      />,
    );

    const rows = screen.getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("3000.0%");
    expect(rows[0]).not.toHaveTextContent("(");
  });

  it("renders headerControl next to the title", () => {
    render(
      <BreakdownChart
        title="By currency"
        data={{ USD: "3000.00" }}
        currency="USD"
        emptyLabel="No data yet"
        headerControl={<button type="button">Switch</button>}
      />,
    );

    expect(screen.getByRole("button", { name: "Switch" })).toBeInTheDocument();
  });

  it("keeps two distinct raw keys as separate slices even when labelFor maps them to the same display label", () => {
    // Grok review round 2 (PR #322): round 1 pre-translated the fallback key
    // by rewriting the source Record's own keys before this component saw
    // it — a user-named group equal to the translated fallback string would
    // silently collapse two slices into one via Object.fromEntries, losing
    // a value. labelFor must only affect the rendered name, not grouping.
    render(
      <BreakdownChart
        title="By group"
        data={{ Ungrouped: "1000.00", "未分组": "500.00" }}
        currency="USD"
        emptyLabel="No data yet"
        labelFor={(key) => (key === "Ungrouped" ? "未分组" : key)}
      />,
    );

    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(2); // both slices survive, not collapsed into one
    const amounts = rows.map((row) => row.textContent);
    expect(amounts.some((text) => text?.includes("1,000.00 USD"))).toBe(true);
    expect(amounts.some((text) => text?.includes("500.00 USD"))).toBe(true);
  });
});
