import { cleanup, render, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { catalogs, type Locale } from "@/locales";
import { GuideBody } from "@/app/guides/_components/guide-body";
import { calculateExample } from "@/app/guides/_lib/example";
import { HomeSections } from "./home-sections";
import { LocaleProvider } from "./locale-provider";

vi.mock("@/lib/api", () => ({
  getPortfolioSummary: vi.fn(),
  getPortfolioPerformance: vi.fn(),
  getPortfolioRisk: vi.fn(),
}));

const locales: Locale[] = ["en", "es", "zh-Hans", "zh-Hant"];
const tools = [
  ["replay", "replay"],
  ["tailRisk", "var"],
  ["stress", "stress"],
  ["style", "style"],
  ["tracking", "tracking"],
] as const;
beforeEach(() => {
  Object.defineProperty(window, "localStorage", { configurable: true, value: { getItem: () => null, setItem: () => {} } });
  window.history.replaceState(null, "", "/");
});
afterEach(cleanup);

function renderHome(locale: Locale) {
  return render(<LocaleProvider routeLocale={locale}><HomeSections /></LocaleProvider>);
}
function jadeSection(container: HTMLElement) {
  const section = container.querySelector<HTMLElement>("#jade-tools");
  expect(section, "#jade-tools exists").not.toBeNull();
  if (!section) throw new Error("Missing #jade-tools");
  return section;
}

it.each(locales)("745 A1 %s places Jade directly between product previews and boundary", locale => {
  const { container } = renderHome(locale);
  const section = jadeSection(container);
  expect(section.previousElementSibling?.id).toBe("product-previews");
  expect(section.nextElementSibling?.id).toBe("boundary");
});

it.each(locales)("745 A2 %s lists five localized tool links and matching summaries in order", locale => {
  const { container } = renderHome(locale);
  const section = jadeSection(container);
  const cards = section.querySelectorAll("ul > li");
  expect(cards).toHaveLength(5);
  const links = Array.from(cards).flatMap(card => Array.from(card.querySelectorAll("a")));
  const prefix = locale === "en" ? "" : `/${locale}`;
  expect(links.map(link => link.getAttribute("href"))).toEqual(tools.map(([, route]) => `${prefix}/guides/jade-${route}`));
  expect(links.map(link => link.textContent)).toEqual(tools.map(([tool]) => catalogs[locale].jade[tool].title));
  tools.forEach(([, summary], index) => {
    expect(cards[index]).toHaveTextContent(catalogs[locale].guides.index.summaries[summary]);
  });
});

it.each(locales)("745 A3 %s links to the localized guides index", locale => {
  const { container } = renderHome(locale);
  const section = jadeSection(container);
  const prefix = locale === "en" ? "" : `/${locale}`;
  expect(within(section).getByRole("link", { name: catalogs[locale].jade.guideLinks.index })).toHaveAttribute("href", `${prefix}/guides`);
});

it.each(locales)("745 A4 %s caption equals the real guide caption", locale => {
  const home = renderHome(locale);
  const section = jadeSection(home.container);
  const guide = render(<LocaleProvider routeLocale={locale}><GuideBody kind="replay" example={calculateExample()} /></LocaleProvider>);
  const homeCaption = section.querySelector("figcaption");
  const guideCaption = guide.container.querySelector("figcaption");
  expect(homeCaption).not.toBeNull();
  expect(guideCaption).not.toBeNull();
  expect(homeCaption?.textContent).toBe(guideCaption?.textContent);
});

it.each(locales)("745 A5 %s contains the replay chart card", locale => {
  const { container } = renderHome(locale);
  const section = jadeSection(container);
  expect(within(section).getByTestId("home-jade-replay-chart")).toBeInTheDocument();
});

it.each(locales)("745 A6 %s ends with the shared holdings CTA", locale => {
  const { container } = renderHome(locale);
  const section = jadeSection(container);
  const lastLink = Array.from(section.querySelectorAll("a")).at(-1);
  expect(lastLink).toHaveAttribute("href", "/holdings");
  expect(lastLink).toHaveTextContent(catalogs[locale].home.hero.ctaPrimary);
  expect(section.lastElementChild?.lastElementChild?.contains(lastLink ?? null)).toBe(true);
});

it.each(locales)("745 A7 %s has four non-empty Jade copy keys", locale => {
  const home: Record<string, unknown> = catalogs[locale].home;
  expect(home).toHaveProperty("jadeTools");
  const copy = home.jadeTools as Record<string, string> | undefined;
  for (const key of ["heading", "body", "ctaHeading", "ctaBody"]) {
    expect(copy?.[key]?.trim().length, key).toBeGreaterThan(0);
  }
});

it("745 A7 English Jade copy contains no whole-word forbidden vocabulary", () => {
  const home: Record<string, unknown> = catalogs.en.home;
  expect(home).toHaveProperty("jadeTools");
  const copy = home.jadeTools as Record<string, string> | undefined;
  const forbidden = /\b(?:recommend|should|buy|sell|hold|reduce|increase|exit|stop-loss|target price|will (?:rise|fall) to|entry point|oversold|overbought|strong buy|(?:bullish|bearish) rating)\b/i;
  for (const value of Object.values(copy ?? {})) expect(value).not.toMatch(forbidden);
});
