import { act, cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { catalogs, type Locale } from "@/locales";
import { formatTickPct } from "@/app/portfolio/performance/_components/performance-format";
import { BENCHMARK_COLORS, PORTFOLIO_COLOR } from "@/app/portfolio/performance/_components/performance-colors";
import { ReplayChart } from "./replay-chart";
import { calculateExample } from "../_lib/example";
import { stressExample, styleExample, trackingExample } from "../_lib/example-batch2";

const locales: Locale[] = ["en", "es", "zh-Hans", "zh-Hant"];
const CHART_W = 800;
const CHART_H = 300;

class SizedResizeObserver {
  private callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) { this.callback = callback; }
  observe(target: Element): void {
    this.callback([{
      target,
      contentRect: { x: 0, y: 0, width: CHART_W, height: CHART_H, top: 0, right: CHART_W, bottom: CHART_H, left: 0, toJSON: () => ({}) },
    } as ResizeObserverEntry], this as unknown as ResizeObserver);
  }
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver = SizedResizeObserver as unknown as typeof ResizeObserver;

beforeEach(() => {
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: { getItem: () => null, setItem: () => {} },
  });
});
afterEach(cleanup);

function percent(locale: string, value: number): string {
  return new Intl.NumberFormat(locale, {
    style: "percent",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}
function fullDate(locale: string, time: number): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" }).format(new Date(time));
}
function legendName(scope: HTMLElement, index: number): string {
  const name = scope.querySelectorAll(".recharts-legend-item-text").item(index).textContent;
  if (!name) throw new Error(`missing legend item ${index}`);
  return name;
}
function plottedX(scope: HTMLElement, index: number): number {
  let best: number[] = [];
  for (const path of scope.querySelectorAll(".recharts-line-curve")) {
    const d = path.getAttribute("d") ?? "";
    const xs = [...d.matchAll(/[ML]\s*(-?\d*\.?\d+)/g)].map((match) => Number(match[1])).filter(Number.isFinite);
    if (xs.length > best.length) best = xs;
  }
  const x = best[index];
  if (x === undefined) throw new Error(`line path has no point ${index} (${best.length} points)`);
  return Math.round(x);
}
function fold(value: string): string {
  return value.replace(/[\s\u202f]+/g, " ").trim();
}
async function showTooltip(scope: HTMLElement, index: number, dateText: string): Promise<HTMLElement> {
  const wrapper = scope.querySelector(".recharts-wrapper");
  if (!(wrapper instanceof HTMLElement)) throw new Error("chart did not render a recharts wrapper");
  wrapper.getBoundingClientRect = () => ({
    x: 0, y: 0, top: 0, left: 0, right: CHART_W, bottom: CHART_H, width: CHART_W, height: CHART_H, toJSON: () => ({}),
  });
  Object.defineProperty(wrapper, "offsetWidth", { configurable: true, value: CHART_W });
  Object.defineProperty(wrapper, "offsetHeight", { configurable: true, value: CHART_H });
  const center = plottedX(scope, index);
  let last = "";
  for (let delta = 0; delta <= 6; delta += 1) {
    for (const clientX of delta === 0 ? [center] : [center + delta, center - delta]) {
      fireEvent.mouseMove(wrapper, { clientX, clientY: 90 });
      await act(async () => {
        await new Promise<void>((resolve) => { requestAnimationFrame(() => resolve()); });
      });
      const tip = scope.querySelector(".public-curve-tooltip");
      last = tip?.textContent ?? "";
      if (tip instanceof HTMLElement && fold(last).includes(fold(dateText))) return tip;
    }
  }
  throw new Error(`tooltip did not show ${dateText} near x=${center}; last=${last}`);
}
function expectSeries(tip: HTMLElement, rows: readonly { name: string; value: string; color: string }[]) {
  const items = within(tip).getAllByRole("listitem");
  expect(items).toHaveLength(rows.length);
  rows.forEach((row, index) => {
    const item = items[index];
    if (!item) throw new Error(`missing series ${row.name}`);
    const text = fold(item.textContent ?? "");
    expect(text).toContain(fold(row.name));
    expect(text).toContain(fold(row.value));
    expect(item.querySelector("[aria-hidden='true']")?.getAttribute("style")).toContain(row.color);
  });
}

it.each(locales)("748 %s homepage and replay guide tooltips match the sample row", async (locale) => {
  const example = calculateExample();
  const last = example.points[example.points.length - 1];
  const first = example.points[0];
  if (!last || !first) throw new Error("missing replay points");
  expect(percent("en", first.portfolio)).toBe("0.00%");
  expect(percent("en", first.spy)).toBe("0.00%");
  expect(percent("en", last.portfolio)).toBe("54.07%");
  expect(percent("en", last.spy)).toBe("91.50%");
  const losAngeles = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeZone: "America/Los_Angeles" }).format(new Date(first.time));
  expect(losAngeles).not.toBe(fullDate("en", first.time));

  const { HomeJadeTools } = await import("@/app/_components/home-jade-tools");
  const { GuideBody } = await import("./guide-body");
  const home = render(<LocaleProvider routeLocale={locale}><HomeJadeTools /></LocaleProvider>);
  const homeTip = await showTooltip(home.container, example.points.length - 1, fullDate(locale, last.time));
  const guide = render(<LocaleProvider routeLocale={locale}><GuideBody kind="replay" example={example} /></LocaleProvider>);
  const guideTip = await showTooltip(guide.container, example.points.length - 1, fullDate(locale, last.time));
  if (locale === "en") expect(legendName(home.container, 0)).toBe("60% SPY + 40% AGG");
  const rows = [
    { name: legendName(home.container, 0), value: percent(locale, last.portfolio), color: PORTFOLIO_COLOR },
    { name: "SPY", value: percent(locale, last.spy), color: BENCHMARK_COLORS.sp500 },
  ];
  for (const tip of [homeTip, guideTip]) {
    expect(tip).toHaveTextContent(fullDate(locale, last.time));
    expect(tip.textContent ?? "").not.toContain(losAngeles);
    expectSeries(tip, rows);
    const style = tip.getAttribute("style") ?? "";
    expect(style).toContain("var(--card)");
    expect(style).toContain("var(--foreground)");
    expect(style).toContain("var(--border)");
  }
  expect(homeTip.textContent).toBe(guideTip.textContent);

  const firstTip = await showTooltip(guide.container, 0, fullDate(locale, first.time));
  expect(firstTip).toHaveTextContent(fullDate(locale, first.time));
  expectSeries(firstTip, [
    { name: legendName(guide.container, 0), value: percent(locale, 0), color: PORTFOLIO_COLOR },
    { name: "SPY", value: percent(locale, 0), color: BENCHMARK_COLORS.sp500 },
  ]);
  cleanup();
});

it.each(locales)("748 %s stress, style and tracking tooltips use each curve's own values", async (locale) => {
  const { Batch2Body } = await import("./batch2-body");
  const stress = stressExample();
  const troughIndex = stress.points.findIndex((point) => point.date === "2025-04-08");
  const trough = stress.points[troughIndex];
  if (!trough || troughIndex < 0) throw new Error("missing stress trough");
  expect(percent("en", trough.portfolio)).toBe("-11.40%");
  expect(percent("en", trough.spy)).toBe("-18.76%");
  const stressView = render(<LocaleProvider routeLocale={locale}><div className="dark" style={{ width: 375 }}><Batch2Body kind="stress" example={stress} /></div></LocaleProvider>);
  const stressTip = await showTooltip(stressView.container, troughIndex, fullDate(locale, trough.time));
  expect(stressTip).toHaveTextContent(fullDate(locale, trough.time));
  expectSeries(stressTip, [
    { name: legendName(stressView.container, 0), value: percent(locale, trough.portfolio), color: PORTFOLIO_COLOR },
    { name: "SPY", value: percent(locale, trough.spy), color: BENCHMARK_COLORS.sp500 },
  ]);
  expect(stressView.container.querySelector(".recharts-tooltip-cursor")).toHaveAttribute("stroke", "var(--foreground)");
  cleanup();

  const style = styleExample();
  const styleLast = style.points[style.points.length - 1];
  if (!styleLast) throw new Error("missing style point");
  expect(formatTickPct(styleLast.portfolio)).toBe("1.24%");
  expect(formatTickPct(styleLast.styleMix)).toBe("1.12%");
  const styleView = render(<LocaleProvider routeLocale={locale}><div className="light" style={{ width: 1024 }}><Batch2Body kind="style" example={style} /></div></LocaleProvider>);
  const styleTip = await showTooltip(styleView.container, style.points.length - 1, fullDate(locale, styleLast.time));
  expect(styleTip).toHaveTextContent(fullDate(locale, styleLast.time));
  expect(legendName(styleView.container, 1)).toBe(catalogs[locale].jade.style.legend.styleMix);
  expectSeries(styleTip, [
    { name: legendName(styleView.container, 0), value: formatTickPct(styleLast.portfolio), color: PORTFOLIO_COLOR },
    { name: catalogs[locale].jade.style.legend.styleMix, value: formatTickPct(styleLast.styleMix), color: "var(--muted-foreground)" },
  ]);
  cleanup();

  const tracking = trackingExample();
  const trackingLast = tracking.points[tracking.points.length - 1];
  if (!trackingLast) throw new Error("missing tracking point");
  expect(percent("en", trackingLast.balanced)).toBe("5.12%");
  expect(percent("en", trackingLast.spy)).toBe("-37.43%");
  const trackingView = render(<LocaleProvider routeLocale={locale}><Batch2Body kind="tracking" example={tracking} /></LocaleProvider>);
  const trackingTip = await showTooltip(trackingView.container, tracking.points.length - 1, fullDate(locale, trackingLast.time));
  expect(trackingTip).toHaveTextContent(fullDate(locale, trackingLast.time));
  expectSeries(trackingTip, [
    { name: catalogs[locale].guides.tracking.balancedLegend, value: percent(locale, trackingLast.balanced), color: PORTFOLIO_COLOR },
    { name: catalogs[locale].guides.tracking.spyLegend, value: percent(locale, trackingLast.spy), color: BENCHMARK_COLORS.sp500 },
  ]);
  cleanup();
});

it("748 hides an empty tooltip and keeps non-finite values off zero and NaN", async () => {
  const time = Date.parse("2024-01-02T00:00:00Z");
  const { container } = render(<LocaleProvider routeLocale="en"><div style={{ width: 375, height: 240 }}>
    <ReplayChart
      points={[{ time, portfolio: Number.NaN, spy: 0 }, { time: time + 86400000, portfolio: -0.114, spy: 0.01 }]}
      peakDate="2024-01-02"
      troughDate="2024-01-03"
      portfolioName="Example"
      formatPercent={(value) => percent("en", value)}
      formatTick={(value) => fullDate("en", value)}
    />
  </div></LocaleProvider>);
  expect(container.querySelector(".public-curve-tooltip")).toBeNull();
  expect(container.querySelectorAll(".recharts-reference-line").length).toBeGreaterThan(0);
  const tip = await showTooltip(container, 0, fullDate("en", time));
  expect(tip).toHaveTextContent(fullDate("en", time));
  expect(tip.textContent ?? "").not.toMatch(/NaN/);
  expectSeries(tip, [
    { name: "Example", value: "\u2014", color: PORTFOLIO_COLOR },
    { name: "SPY", value: "0.00%", color: BENCHMARK_COLORS.sp500 },
  ]);
  const wrapper = container.querySelector(".recharts-wrapper");
  if (!(wrapper instanceof HTMLElement)) throw new Error("missing wrapper");
  fireEvent.mouseLeave(wrapper);
  await waitFor(() => {
    expect(container.querySelector(".public-curve-tooltip")).toBeNull();
  });
});
