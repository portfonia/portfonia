import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { formatAxisDate } from "@/app/portfolio/performance/_components/performance-format";
import { catalogs } from "@/locales";
import { TechnicalPageBody } from "./technical-page-body";

const { getTechnicalTable, getTechnicalSeries } = vi.hoisted(() => ({ getTechnicalTable: vi.fn(), getTechnicalSeries: vi.fn() }));
vi.mock("@/lib/api", () => ({ getTechnicalTable, getTechnicalSeries }));
const { lineProps, xAxisProps, legend } = vi.hoisted(() => ({
  lineProps: [] as { dataKey: string; stroke: string }[],
  xAxisProps: [] as { tickFormatter?: (value: string) => string }[],
  legend: vi.fn(() => null),
}));
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => children,
  LineChart: ({ children }: { children: React.ReactNode }) => children,
  Line: (props: { dataKey: string; stroke: string }) => { lineProps.push(props); return null; },
  XAxis: (props: { tickFormatter?: (value: string) => string }) => { xAxisProps.push(props); return null; },
  CartesianGrid: () => null, YAxis: () => null, Tooltip: () => null, Legend: legend,
}));
beforeEach(() => {
  vi.clearAllMocks();
  lineProps.length = 0;
  xAxisProps.length = 0;
});

const chartColors = ["var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)"];
const pointDates = ["2026-10-01", "2026-10-26"];
async function renderSeries() {
  const table = { as_of: "2026-10-26", holdings_date: "2026-10-26", positions: [{ ticker: "AAPL", name: "Apple", last_close: 120, bars: 252, pct_vs_sma50: 0.052, pct_vs_sma200: null, range_52w_low: 80, range_52w_high: 140, pct_in_52w_range: 0.67, vol_20d_annualized: 0.42 }] };
  getTechnicalSeries.mockImplementation(async (ticker: string, start: string, end: string) => ({
    ticker, name: "Apple", start, end,
    points: pointDates.map(date => ({ date, pct_vs_sma50: 0.05, pct_vs_sma200: 0.1, pct_in_52w_range: 0.67, vol_20d_annualized: 0.42 })),
  }));
  render(<LocaleProvider routeLocale="en"><TechnicalPageBody initialTable={table} initialLoadError={false} /></LocaleProvider>);
  await waitFor(() => expect(lineProps).toHaveLength(4));
}

it("850 uses chart tokens for all four metrics in order", async () => {
  await renderSeries();
  expect(lineProps.map(({ dataKey, stroke }) => [dataKey, stroke])).toEqual([
    ["pct_vs_sma50", chartColors[0]], ["pct_vs_sma200", chartColors[1]],
    ["pct_in_52w_range", chartColors[2]], ["vol_20d_annualized", chartColors[3]],
  ]);
});

it("850 renders four line swatches without the Recharts legend", async () => {
  await renderSeries();
  const items = within(screen.getByRole("list")).getAllByRole("listitem");
  expect(items).toHaveLength(4);
  expect(items.map(item => item.querySelector("span")?.style.backgroundColor)).toEqual(chartColors);
  expect(legend).not.toHaveBeenCalled();
});

it("850 formats date axis ticks with the shared locale formatter", async () => {
  await renderSeries();
  const spanDays = (Date.parse(pointDates[1]) - Date.parse(pointDates[0])) / 86_400_000;
  const formatted = xAxisProps[0].tickFormatter?.(pointDates[0]);
  expect(formatted).toBe(formatAxisDate(pointDates[0], "en", spanDays));
  expect(formatted).not.toBe(pointDates[0]);
});

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
