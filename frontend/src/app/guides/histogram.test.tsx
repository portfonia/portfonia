import type { ReactNode } from "react";
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { BarChart, XAxis } from "recharts";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { GuideBody } from "./_components/guide-body";
import { calculateExample } from "./_lib/example";

vi.mock("recharts", async () => ({
  ...await vi.importActual<typeof import("recharts")>("recharts"),
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  BarChart: vi.fn(({ children }: { children: ReactNode }) => <div>{children}</div>),
  XAxis: vi.fn(() => null),
}));

afterEach(cleanup);

it("737 histogram plots bin midpoints while captions retain edges", () => {
  const example = calculateExample();
  const { container } = render(
    <LocaleProvider routeLocale="en">
      <GuideBody kind="var" example={example} />
    </LocaleProvider>
  );
  const chartData = vi.mocked(BarChart).mock.calls[0]?.[0].data;
  expect(chartData?.[0]).toMatchObject({ index: -8, lower: -0.04, midpoint: -0.0375 });
  expect(chartData).toHaveLength(example.histogram.length);
  expect(vi.mocked(XAxis).mock.calls[0]?.[0].dataKey).toBe("midpoint");
  expect(container.querySelector("figcaption")?.textContent).toContain("-4.00% \u2013 -3.50%: 1");
});
