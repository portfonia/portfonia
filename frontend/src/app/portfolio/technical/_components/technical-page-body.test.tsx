import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { catalogs } from "@/locales";
import { TechnicalPageBody } from "./technical-page-body";

const { getTechnicalTable, getTechnicalSeries } = vi.hoisted(() => ({ getTechnicalTable: vi.fn(), getTechnicalSeries: vi.fn() }));
vi.mock("@/lib/api", () => ({ getTechnicalTable, getTechnicalSeries }));
vi.mock("recharts", () => ({ ResponsiveContainer: ({ children }: { children: React.ReactNode }) => children, LineChart: () => null, Line: () => null, CartesianGrid: () => null, XAxis: () => null, YAxis: () => null, Tooltip: () => null, Legend: () => null }));

it("829 renders a technical table and rejects more than 92 days without a request", async () => {
  const table = { as_of: "2026-10-26", holdings_date: "2026-10-26", positions: [{ ticker: "AAPL", name: "Apple", last_close: 120, bars: 252, pct_vs_sma50: 0.052, pct_vs_sma200: null, range_52w_low: 80, range_52w_high: 140, pct_in_52w_range: 0.67, vol_20d_annualized: 0.42 }] };
  getTechnicalTable.mockResolvedValue(table);
  getTechnicalSeries.mockResolvedValue({ ticker: "AAPL", name: "Apple", start: "2026-07-27", end: "2026-10-26", points: [] });
  render(<LocaleProvider routeLocale={null}><TechnicalPageBody initialTable={table} initialLoadError={false} /></LocaleProvider>);
  expect(screen.getByRole("table")).toHaveTextContent("AAPL");
  expect(screen.getByRole("table")).toHaveTextContent("+5.2%");
  await waitFor(() => expect(getTechnicalSeries).toHaveBeenCalled());
  getTechnicalSeries.mockClear();
  fireEvent.change(screen.getByLabelText(catalogs.en.portfolio.technical.start), { target: { value: "2026-01-01" } });
  fireEvent.change(screen.getByLabelText(catalogs.en.portfolio.technical.end), { target: { value: "2026-04-03" } });
  expect(screen.getByRole("alert")).toHaveTextContent(catalogs.en.portfolio.technical.rangeTooLong);
  expect(getTechnicalSeries).not.toHaveBeenCalled();
});
