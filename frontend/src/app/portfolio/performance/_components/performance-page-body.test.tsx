import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

// lib/api.ts imports @/lib/auth-actions at module top, whose own import of
// lib/supabase/server hits the `server-only` guard outside Next's compiler
// (same reason get-started-menu.test.tsx mocks it).
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import {
  type HoldingValueOut,
  type PortfolioPerformanceResponse,
  type PortfolioPerformanceSeries,
  type PortfolioSummary,
  getPortfolioPerformance,
} from "@/lib/api";
import { PerformancePageBody } from "./performance-page-body";

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, getPortfolioPerformance: vi.fn() };
});

const getPerformanceMock = vi.mocked(getPortfolioPerformance);

function holding(overrides: Partial<HoldingValueOut>): HoldingValueOut {
  return {
    holding_id: "11111111-1111-1111-1111-111111111111",
    name: "Holding",
    ticker: null,
    fund_code: null,
    currency: "USD",
    asset_type: null,
    asset_class: null,
    sector: null,
    market: "US",
    market_value: null,
    market_value_base: null,
    price_as_of: null,
    pricing_mode: "manual",
    capture_supported: true,
    broker: null,
    account: null,
    portfolio: null,
    avg_cost: null,
    shares: null,
    notes: null,
    cost_basis_base: null,
    unrealized_pnl_base: null,
    unrealized_pnl_pct: null,
    ...overrides,
  };
}

function summary(holdings: HoldingValueOut[]): PortfolioSummary {
  return {
    base_currency: "USD",
    fx_rates_as_of: {},
    total_base: "0.00",
    by_market: {},
    by_currency: {},
    by_asset_type: {},
    by_sector: {},
    by_asset_class: {},
    by_group: {},
    by_broker: {},
    by_account: {},
    total_cost_basis_base: "0.00",
    total_unrealized_pnl_base: "0.00",
    total_unrealized_pnl_pct: null,
    price_as_of_date: null,
    stale_tickers: [],
    holdings,
  };
}

const TRACKED_SUMMARY = summary([
  holding({ market: "US", broker: "IB", account: "Main", portfolio: "Equity" }),
  holding({
    holding_id: "22222222-2222-2222-2222-222222222222",
    market: "HK",
    broker: "IB",
    account: "Main",
    portfolio: "Equity",
  }),
]);

function portfolioSeries(
  points: PortfolioPerformanceSeries["points"],
  overrides: Partial<PortfolioPerformanceSeries> = {},
): PortfolioPerformanceSeries {
  return {
    empty: false,
    start_date: points[0]?.date ?? null,
    end_date: points.at(-1)?.date ?? null,
    tracking_start: points[0]?.date ?? null,
    points,
    quality_flags: [],
    ...overrides,
  };
}

function response(overrides: Partial<PortfolioPerformanceResponse> = {}): PortfolioPerformanceResponse {
  const series = portfolioSeries([
    { date: "2026-08-03", value_base: "100", return_pct_cumulative: "0", is_approximate: false },
    { date: "2026-08-04", value_base: "110", return_pct_cumulative: "0.1", is_approximate: false },
    { date: "2026-08-05", value_base: "121", return_pct_cumulative: "0.21", is_approximate: false },
  ]);
  return {
    portfolio: series,
    benchmarks: [
      {
        index_code: "sp500",
        name: "S&P 500",
        start_date: "2026-08-03",
        points: [
          {
            date: "2026-08-03",
            return_pct_cumulative: "0",
            price_as_of: "2026-08-03",
            fx_as_of: {},
            carried: false,
            unavailable_reason: null,
          },
          {
            date: "2026-08-04",
            return_pct_cumulative: "0.01",
            price_as_of: "2026-08-04",
            fx_as_of: {},
            carried: false,
            unavailable_reason: null,
          },
          {
            date: "2026-08-05",
            return_pct_cumulative: "0.03",
            price_as_of: "2026-08-05",
            fx_as_of: {},
            carried: false,
            unavailable_reason: null,
          },
        ],
        comparable: true,
        displayable: true,
        normalization: "portfolio_start",
        anchor_date: "2026-08-03",
        display_start_date: "2026-08-03",
        display_end_date: "2026-08-05",
        comparison_start: "2026-08-03",
        comparison_end: "2026-08-05",
        comparison_status: "available",
        comparison_return_pct: "0.03",
      },
      {
        index_code: "nasdaq",
        name: "Nasdaq Composite",
        start_date: "2026-08-03",
        points: [],
        comparable: false,
        displayable: false,
        normalization: "unavailable",
        anchor_date: null,
        display_start_date: null,
        display_end_date: null,
        comparison_start: "2026-08-03",
        comparison_end: "2026-08-05",
        comparison_status: "anchor_unavailable",
        comparison_return_pct: null,
      },
    ],
    header: {
      value_base: "121.00",
      value_change_base: "21.00",
      value_change_pct: "0.21",
      label: "market_value_change",
    },
    meta: { range: "1Y", twr: true, base_currency: "USD", filters: {} },
    ...overrides,
  };
}

function renderBody(initialSummary = TRACKED_SUMMARY) {
  render(
    <LocaleProvider>
      <PerformancePageBody initialSummary={initialSummary} initialLoadError={false} />
    </LocaleProvider>,
  );
}

async function waitForChart() {
  return screen.findByTestId("performance-chart");
}

describe("PerformancePageBody", () => {
  beforeEach(() => {
    getPerformanceMock.mockReset();
    getPerformanceMock.mockResolvedValue(response());
  });

  it("fetches with defaults (1Y, TWR on, all benchmarks, user currency) and draws chart + metrics", async () => {
    renderBody();
    await waitForChart();

    expect(getPerformanceMock).toHaveBeenCalledTimes(1);
    expect(getPerformanceMock).toHaveBeenCalledWith({
      range: "1Y",
      twr: true,
      benchmarks: ["sp500", "dow30", "nasdaq"],
      markets: [],
      groups: [],
      brokers: [],
      accounts: [],
      baseCurrency: "USD",
    });
    // Metrics card: end value, market-value-change $, and the TWR % label.
    expect(screen.getByText("Value at end of period")).toBeInTheDocument();
    expect(screen.getByText("Market value change")).toBeInTheDocument();
    expect(screen.getByText(/Approx\. TWR since/)).toBeInTheDocument();
    expect(screen.getByText("+21.00%")).toBeInTheDocument();
    expect(screen.queryByTestId("portfolio-empty-state")).not.toBeInTheDocument();
    expect(screen.getByText(/selected indexes keep their available history/i)).toBeInTheDocument();
    const legend = screen.getByTestId("chart-legend");
    expect(within(legend).getByText("Portfolio")).toBeInTheDocument();
    expect(within(legend).getByText("S&P 500")).toBeInTheDocument();
    expect(
      screen.getByText(/Nasdaq Composite has no usable index history in this range/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Index history is relative to Aug 3, 2026/)).toBeInTheDocument();
  });

  it("shows tracking-since copy with the tracking start date", async () => {
    renderBody();
    await waitForChart();

    expect(screen.getByText(/Portfolio tracked since Aug 3, 2026/)).toBeInTheDocument();
  });

  it("refetches when the range changes", async () => {
    const user = userEvent.setup();
    renderBody();
    await waitForChart();

    await user.click(screen.getByRole("button", { name: "6M" }));

    await waitFor(() => expect(getPerformanceMock).toHaveBeenCalledTimes(2));
    expect(getPerformanceMock.mock.calls[1][0].range).toBe("6M");
  });

  it("switches to raw market-value wording when TWR is off", async () => {
    const user = userEvent.setup();
    renderBody();
    await waitForChart();

    await user.click(screen.getByRole("button", { name: "Market value" }));

    await waitFor(() =>
      expect(screen.getByText("Change vs. period start")).toBeInTheDocument(),
    );
    expect(getPerformanceMock.mock.calls.at(-1)?.[0].twr).toBe(false);
    expect(
      screen.getByText(/neither removes the effect of deposits or withdrawals/),
    ).toBeInTheDocument();
  });

  it("removes a benchmark from the request when unchecked", async () => {
    const user = userEvent.setup();
    renderBody();
    await waitForChart();

    await user.click(screen.getByRole("button", { name: /Benchmarks/ }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    await user.click(screen.getByRole("menuitemcheckbox", { name: "S&P 500" }));

    await waitFor(() => expect(getPerformanceMock).toHaveBeenCalledTimes(2));
    expect(getPerformanceMock.mock.calls[1][0].benchmarks).toEqual(["dow30", "nasdaq"]);
  });

  it("Benchmarks All reselects every code and never sends an empty benchmark list (review finding 1)", async () => {
    const user = userEvent.setup();
    renderBody();
    await waitForChart();
    // Initial request carries all three codes (default state).
    expect(getPerformanceMock.mock.calls[0][0].benchmarks).toEqual([
      "sp500",
      "dow30",
      "nasdaq",
    ]);

    // Drop two benchmarks so All is no longer the active state.
    await user.click(screen.getByRole("button", { name: /Benchmarks/ }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    await user.click(screen.getByRole("menuitemcheckbox", { name: "S&P 500" }));
    await user.click(screen.getByRole("menuitemcheckbox", { name: "Dow 30" }));
    await waitFor(() => expect(getPerformanceMock).toHaveBeenCalledTimes(3));

    // Clicking All restores every code — never [].
    await user.click(screen.getByRole("menuitemcheckbox", { name: "All" }));
    await waitFor(() => expect(getPerformanceMock).toHaveBeenCalledTimes(4));
    expect(getPerformanceMock.mock.calls[3][0].benchmarks).toEqual([
      "sp500",
      "dow30",
      "nasdaq",
    ]);
    for (const call of getPerformanceMock.mock.calls) {
      expect(call[0].benchmarks.length).toBeGreaterThan(0);
    }
  });

  it("Benchmarks All while every code is already selected does not refetch", async () => {
    const user = userEvent.setup();
    renderBody();
    await waitForChart();
    expect(getPerformanceMock).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /Benchmarks/ }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    expect(screen.getByRole("menuitemcheckbox", { name: "All" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await user.click(screen.getByRole("menuitemcheckbox", { name: "All" }));

    expect(getPerformanceMock).toHaveBeenCalledTimes(1);
  });

  it("dimension-menu All still means omit (empty selection = no filter)", async () => {
    const user = userEvent.setup();
    renderBody();
    await waitForChart();

    await user.click(screen.getByRole("button", { name: /Markets/ }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    await user.click(screen.getByRole("menuitemcheckbox", { name: "HK" }));
    await waitFor(() => expect(getPerformanceMock).toHaveBeenCalledTimes(2));
    expect(getPerformanceMock.mock.calls[1][0].markets).toEqual(["HK"]);

    // All row (still open after the HK toggle) clears back to the omitted
    // param — unchanged dimension semantics.
    await user.click(screen.getByRole("menuitemcheckbox", { name: "All" }));
    await waitFor(() => expect(getPerformanceMock).toHaveBeenCalledTimes(3));
    expect(getPerformanceMock.mock.calls[2][0].markets).toEqual([]);
  });

  it("filters by a dimension value and shows the reset affordance", async () => {
    const user = userEvent.setup();
    renderBody();
    await waitForChart();

    await user.click(screen.getByRole("button", { name: /Markets/ }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    await user.click(screen.getByRole("menuitemcheckbox", { name: "HK" }));

    await waitFor(() => expect(getPerformanceMock).toHaveBeenCalledTimes(2));
    expect(getPerformanceMock.mock.calls[1][0]).toMatchObject({ markets: ["HK"] });

    const reset = screen.getByRole("button", { name: "Reset filters" });
    await user.click(reset);

    await waitFor(() => expect(getPerformanceMock).toHaveBeenCalledTimes(3));
    expect(getPerformanceMock.mock.calls[2][0]).toMatchObject({ markets: [] });
  });

  it("keeps benchmarks and shows the empty state when portfolio history is empty", async () => {
    getPerformanceMock.mockResolvedValue(
      response({
        portfolio: portfolioSeries([], {
          empty: true,
          start_date: null,
          end_date: null,
          tracking_start: null,
          quality_flags: [],
        }),
        header: {
          value_base: "0",
          value_change_base: "0",
          value_change_pct: "0",
          label: "market_value_change",
        },
      }),
    );
    renderBody();
    await waitForChart();

    expect(screen.getByTestId("portfolio-empty-state")).toHaveTextContent(
      "No portfolio history in this range yet",
    );
    // Benchmarks still draw — never blank the whole chart (requirement 5).
    expect(screen.getByTestId("chart-legend")).toHaveTextContent("S&P 500");
    expect(screen.queryByText("Period summary")).not.toBeInTheDocument();
    // Empty-portfolio description says benchmarks self-normalize (D7).
    expect(screen.getByText(/benchmarks are normalized over their own data/)).toBeInTheDocument();
  });

  it("switches the empty copy to the filtered variant once a filter is active", async () => {
    getPerformanceMock.mockResolvedValue(
      response({
        portfolio: portfolioSeries([], {
          empty: true,
          start_date: null,
          end_date: null,
          tracking_start: "2026-08-01",
          quality_flags: [],
        }),
      }),
    );
    const user = userEvent.setup();
    renderBody();
    await waitForChart();

    await user.click(screen.getByRole("button", { name: /Markets/ }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    await user.click(screen.getByRole("menuitemcheckbox", { name: "HK" }));

    await waitFor(() =>
      expect(screen.getByTestId("portfolio-empty-state")).toHaveTextContent(
        "Nothing matches the selected filters in this range",
      ),
    );
  });

  it("shows the approximate badge and dashed legend hint for approximate data", async () => {
    getPerformanceMock.mockResolvedValue(
      response({
        portfolio: portfolioSeries(
          [
            {
              date: "2026-08-03",
              value_base: "100",
              return_pct_cumulative: "0",
              is_approximate: false,
            },
            {
              date: "2026-08-04",
              value_base: "101",
              return_pct_cumulative: "0.01",
              is_approximate: true,
            },
          ],
          { quality_flags: ["approx_fx"] },
        ),
      }),
    );
    renderBody();

    expect(await screen.findByTestId("approx-badge")).toHaveTextContent("Approximate data");
    expect(screen.getByText(/fallback exchange rates/)).toBeInTheDocument();
    expect(screen.getByText(/dashed = approximate/)).toBeInTheDocument();
  });

  it("shows an error with a working Retry when the fetch fails", async () => {
    getPerformanceMock.mockRejectedValueOnce(new Error("boom"));
    const user = userEvent.setup();
    renderBody();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't load performance data.",
    );

    await user.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(getPerformanceMock).toHaveBeenCalledTimes(2));
    expect(await screen.findByTestId("performance-chart")).toBeInTheDocument();
  });

  it("refetches in the switched base currency", async () => {
    const user = userEvent.setup();
    renderBody();
    await waitForChart();

    await user.click(screen.getByRole("button", { name: /base currency/i }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    await user.click(screen.getByRole("menuitem", { name: "CNY" }));

    await waitFor(() => expect(getPerformanceMock).toHaveBeenCalledTimes(2));
    expect(getPerformanceMock.mock.calls[1][0].baseCurrency).toBe("CNY");
  });

  it("draws non-comparable displayable history and explains the own baseline", async () => {
    getPerformanceMock.mockResolvedValue(
      response({
        benchmarks: [
          {
            index_code: "sp500",
            name: "S&P 500",
            start_date: "2026-08-04",
            points: [
              {
                date: "2026-08-04",
                return_pct_cumulative: "0",
                price_as_of: "2026-08-04",
                fx_as_of: {},
                carried: false,
                unavailable_reason: null,
              },
            ],
            comparable: false,
            displayable: true,
            normalization: "own_start",
            anchor_date: "2026-08-04",
            display_start_date: "2026-08-04",
            display_end_date: "2026-08-04",
            comparison_start: "2026-08-03",
            comparison_end: "2026-08-05",
            comparison_status: "anchor_unavailable",
            comparison_return_pct: null,
          },
        ],
      }),
    );
    renderBody();
    await waitForChart();
    expect(screen.getByTestId("chart-legend")).toHaveTextContent("S&P 500");
    expect(screen.getByText(/cannot be valued on the portfolio's first tracked day/)).toBeInTheDocument();
    expect(screen.queryByText(/has no usable index history/)).not.toBeInTheDocument();
  });

  it("explains a first-snapshot 0% baseline without hiding the index", async () => {
    getPerformanceMock.mockResolvedValue(
      response({
        portfolio: portfolioSeries([
          { date: "2026-09-07", value_base: "1000", return_pct_cumulative: "0", is_approximate: false },
        ]),
        benchmarks: [
          {
            index_code: "sp500",
            name: "S&P 500",
            start_date: "2026-09-03",
            points: [
              {
                date: "2026-09-03",
                return_pct_cumulative: "-0.0909",
                price_as_of: "2026-09-03",
                fx_as_of: {},
                carried: false,
                unavailable_reason: null,
              },
              {
                date: "2026-09-07",
                return_pct_cumulative: "0",
                price_as_of: "2026-09-04",
                fx_as_of: {},
                carried: true,
                unavailable_reason: null,
              },
            ],
            comparable: true,
            displayable: true,
            normalization: "portfolio_start",
            anchor_date: "2026-09-07",
            display_start_date: "2026-09-03",
            display_end_date: "2026-09-07",
            comparison_start: "2026-09-07",
            comparison_end: "2026-09-07",
            comparison_status: "baseline_only",
            comparison_return_pct: "0.0000",
          },
        ],
      }),
    );
    renderBody();
    await waitForChart();
    expect(screen.getByText(/first tracked snapshot; change starts at 0%/)).toBeInTheDocument();
    expect(screen.getByTestId("chart-legend")).toHaveTextContent("S&P 500");
  });
});
