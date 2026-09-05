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
  it("states the closing-price date when one is available, framed as each market's own close (issue #350 item 6)", () => {
    // price_as_of_date is max(used_trade_dates) collapsed across every
    // market a holding was priced in (each captured using that market's own
    // local clock) — a bare date with no framing would misread as one
    // single global timestamp when a book spans, say, US and Asia listings.
    renderBanner("2026-01-09");
    expect(
      screen.getByText(
        "Prices as of 2026-01-09 — each market's own latest close, not one global timestamp. End-of-day data, not real-time.",
      ),
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
        "No per-market closing prices captured yet for this view. End-of-day data, not real-time.",
      ),
    ).toBeInTheDocument();
  });

  it("does not attribute the None case to cash/wealth-management specifically", () => {
    // Grok review round 2 (PR #322): round-1's copy for this case named
    // "cash and wealth-management values" — but price_as_of_date is also
    // None for an empty book, a capture-unsupported-only book, or an auto
    // holding still waiting on its first snapshot, none of which are cash.
    renderBanner(null);
    expect(screen.queryByText(/cash/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/wealth-management/i)).not.toBeInTheDocument();
  });
});
