import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { BENCHMARK_COLORS, PORTFOLIO_COLOR } from "@/app/portfolio/performance/_components/performance-colors";
import { ReplayChart, ReplayLegend } from "./replay-chart";
const { legend } = vi.hoisted(() => ({ legend: vi.fn() }));
vi.mock("recharts", async () => {
 const actual = await vi.importActual<typeof import("recharts")>("recharts");
 const Container = ({ children }: { children: import("react").ReactNode }) => <div>{children}</div>;
 return { ...actual, ResponsiveContainer: Container, LineChart: Container, Legend: () => { legend(); return null; } };
});
afterEach(cleanup);
it("852 A2 renders the two replay swatches outside the chart without a Recharts legend", () => {
 render(<><ReplayChart points={[]} peakDate="2025-01-01" troughDate="2025-01-02" portfolioName="Portfolio" formatPercent={String} formatTick={String} /><ReplayLegend portfolioName="Portfolio" /></>);
 const items = screen.getAllByRole("listitem");
 expect(items.map(item => item.textContent)).toEqual(["Portfolio", "SPY"]);
 expect(items[0].querySelector("span")).toHaveStyle({ backgroundColor: PORTFOLIO_COLOR });
 expect(items[1].querySelector("span")).toHaveStyle({ backgroundColor: BENCHMARK_COLORS.sp500 });
 expect(legend).not.toHaveBeenCalled();
});
