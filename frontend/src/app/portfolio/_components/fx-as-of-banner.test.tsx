import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { FxAsOfBanner } from "./fx-as-of-banner";

function renderBanner(fxRatesAsOf: Record<string, string>) {
  return render(
    <LocaleProvider>
      <FxAsOfBanner fxRatesAsOf={fxRatesAsOf} />
    </LocaleProvider>,
  );
}

describe("FxAsOfBanner", () => {
  it("renders nothing when no conversion happened (empty map)", () => {
    const { container } = renderBanner({});
    expect(container).toBeEmptyDOMElement();
  });

  it("lists each currency with its own date, sorted", () => {
    renderBanner({ HKD: "2026-09-03", CNY: "2026-09-04" });
    expect(screen.getByText(/CNY as of 2026-09-04/)).toBeInTheDocument();
    expect(screen.getByText(/HKD as of 2026-09-03/)).toBeInTheDocument();
  });

  it("shows different dates for different currencies without collapsing them (issue #354)", () => {
    renderBanner({ HKD: "2026-09-03", CNY: "2026-09-04" });
    const text = screen.getByText(/CNY as of/).textContent ?? "";
    expect(text).toContain("2026-09-03");
    expect(text).toContain("2026-09-04");
  });
});
