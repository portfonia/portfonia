import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { PriceAsOfBanner } from "./price-as-of-banner";

function renderBanner(priceAsOfDate: string | null) {
  return render(
    <LocaleProvider>
      <PriceAsOfBanner priceAsOfDate={priceAsOfDate} />
    </LocaleProvider>,
  );
}

describe("PriceAsOfBanner", () => {
  it("states the closing-price date when one is available", () => {
    renderBanner("2026-01-09");
    expect(
      screen.getByText("Prices as of 2026-01-09 close — end-of-day data, not real-time."),
    ).toBeInTheDocument();
  });

  it("does not claim 'no priced holdings' when there is simply no captured close (e.g. a cash-only book)", () => {
    // Grok review round 1 (PR #322): price_as_of_date is None whenever no
    // captured close was used — including a cash/wmf-only portfolio, which
    // still has a non-zero total. The old copy read "No priced holdings
    // yet", directly contradicting the total assets figure on the same page.
    renderBanner(null);
    expect(screen.queryByText(/no priced holdings/i)).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "No exchange closing prices in this view — cash and wealth-management values are what you entered. End-of-day data, not real-time.",
      ),
    ).toBeInTheDocument();
  });
});
