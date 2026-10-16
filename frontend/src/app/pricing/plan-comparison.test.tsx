import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { LegalDocument } from "@/app/_components/legal-document";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { catalogs, type Locale } from "@/locales";
import PricingPage from "./page";
import { localizedPath } from "@/lib/seo";

function renderPricing(locale: Locale = "en") {
  window.history.replaceState(null, "", locale === "en" ? "/pricing" : `/${locale}/pricing`);
  render(<LocaleProvider routeLocale={locale === "en" ? null : locale}><PricingPage /></LocaleProvider>);
}

afterEach(() => window.history.replaceState(null, "", "/"));

function comparison() {
  return screen.getByRole("region", { name: catalogs.en.pricingPlans.heading });
}

it("A1 comparison follows the intro and precedes section one", () => {
  renderPricing();
  const intro = screen.getByText(catalogs.en.legal.pricing.intro);
  const heading = screen.getByRole("heading", { level: 2, name: catalogs.en.pricingPlans.heading });
  const firstSection = screen.getByRole("heading", { level: 2, name: catalogs.en.legal.pricing.sections[0].heading });
  expect(intro.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(heading.compareDocumentPosition(firstSection) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

it("A2 English cards preserve order, exact prices, leads and feature counts", () => {
  renderPricing();
  const headings = within(comparison()).getAllByRole("heading", { level: 3 });
  expect(headings.map(h => h.textContent)).toEqual(["Briefing", "Advanced", "Jade"]);
  const cards = within(comparison()).getAllByRole("region");
  expect(cards.map(card => card.getAttribute("data-plan"))).toEqual(["briefing", "advanced", "jade"]);
  cards.forEach((card, i) => {
    const plan = catalogs.en.pricingPlans.plans[i];
    expect(within(card).getAllByRole("listitem").map(li => li.textContent)).toEqual(plan.features.map(s => s.replace(/<\/?[a-z]+>/g, "")));
    expect(within(card).getAllByRole("listitem")).toHaveLength([6, 3, 9][i]);
    expect(Array.from(card.querySelectorAll("p")).map(p => p.textContent)).toEqual([...plan.prices, ...(plan.lead ? [plan.lead] : [])]);
  });
});

it.each(["zh-Hans", "zh-Hant"] as const)("A3 %s renders catalog names and Jade features", locale => {
  renderPricing(locale);
  const block = screen.getByRole("region", { name: catalogs[locale].pricingPlans.heading });
  const headings = within(block).getAllByRole("heading", { level: 3 });
  expect(headings.map(h => h.textContent)).toEqual(catalogs[locale].pricingPlans.plans.map(plan => plan.name));
  const jade = within(block).getAllByRole("region")[2];
  expect(within(jade).getAllByRole("listitem").map(li => li.textContent)).toEqual(catalogs[locale].pricingPlans.plans[2].features.map(s => s.replace(/<\/?[a-z]+>/g, "")));
});

it("A4 only the Jade name header has jade-surface", () => {
  renderPricing();
  const headings = within(comparison()).getAllByRole("heading", { level: 3 });
  expect(headings[2]).toHaveClass("jade-surface");
  expect(headings[0]).not.toHaveClass("jade-surface");
  expect(headings[1]).not.toHaveClass("jade-surface");
});

it("A5 comparison renders the credit footnote", () => {
  renderPricing();
  expect(within(comparison()).getByText(catalogs.en.pricingPlans.footnote)).toBeInTheDocument();
});

it.each(["en", "es", "zh-Hans", "zh-Hant"] as const)("A6 %s comparison contains no buttons and exactly six Jade links", locale => {
  renderPricing(locale);
  const formerBuyCreditsLabel = { en: "Buy credits", es: "Comprar cr\u00e9ditos", "zh-Hans": "\u8d2d\u4e70 credits", "zh-Hant": "\u8cfc\u8cb7 credits" }[locale];
  expect(screen.queryByRole("link", { name: formerBuyCreditsLabel })).not.toBeInTheDocument();
  const block = screen.getByRole("region", { name: catalogs[locale].pricingPlans.heading });
  expect(block.querySelectorAll("button")).toHaveLength(0);
  const links = Array.from(block.querySelectorAll("a"));
  expect(links).toHaveLength(6);
  const advanced = block.querySelector('[data-plan="advanced"]');
  expect(advanced?.querySelectorAll("a")).toHaveLength(0);
  const jade = block.querySelector('[data-plan="jade"]');
  for (const link of links) expect(jade).toContainElement(link);
  expect(links.map(link => link.getAttribute("href"))).toEqual([
    localizedPath("/guides/jade-replay", locale),
    localizedPath("/guides/jade-var", locale),
    localizedPath("/guides/jade-stress", locale),
    localizedPath("/guides/jade-style", locale),
    localizedPath("/guides/jade-tracking", locale),
    localizedPath("/agent/guide", locale),
  ]);
  const guidePhrase = {"en": "Programmatic holdings writes", "es": "Escritura program\u00e1tica de posiciones", "zh-Hans": "\u7a0b\u5e8f\u5316\u6301\u4ed3\u5199\u5165", "zh-Hant": "\u7a0b\u5f0f\u5316\u6301\u5009\u5beb\u5165"}[locale];
  expect(links[5].textContent).toBe(guidePhrase);
  expect(links[5].closest("li")?.textContent?.length).toBeGreaterThan(guidePhrase.length);
});

it("A7 cards stack below the three-column medium breakpoint", () => {
  renderPricing();
  const card = within(comparison()).getAllByRole("region")[0];
  expect(card.parentElement).toHaveClass("grid", "grid-cols-1", "md:grid-cols-3");
});

it.each(["en", "zh-Hans", "zh-Hant"] as const)("A8 %s plan strings stay non-advisory", locale => {
  const c = catalogs[locale];
  const strings: string[] = [];
  function collect(value: unknown) {
    if (typeof value === "string") strings.push(value);
    else if (value && typeof value === "object") Object.values(value).forEach(collect);
  }
  collect(c.pricingPlans);
  strings.push(c.legal.pricing.sections[0].body[3]);
  expect(strings.join(" ")).not.toMatch(/\b(should|recommend\w*|buy|sell)\b|\u5efa\u8bae|\u5efa\u8b70|\u4e70\u5165|\u8cb7\u5165|\u5356\u51fa|\u8ce3\u51fa/i);
});

it("A10 Terms renders without a comparison heading", () => {
  render(<LocaleProvider routeLocale={null}><LegalDocument doc="terms" /></LocaleProvider>);
  expect(screen.queryByRole("heading", { name: "Plans at a glance" })).not.toBeInTheDocument();
});

it("770 Jade comparison renders Agent risk-result access", () => {
  renderPricing();
  const jade = comparison().querySelector('[data-plan="jade"]');
  expect(jade).not.toBeNull();
  expect(within(jade as HTMLElement).getByText("Your AI agent can read these risk results")).toBeInTheDocument();
});


it("775 programmatic holdings writes belong to Jade", () => {
  renderPricing();
  const block = comparison();
  const advanced = block.querySelector('[data-plan="advanced"]');
  const jade = block.querySelector('[data-plan="jade"]');
  expect(advanced).not.toHaveTextContent("Programmatic holdings writes");
  expect(jade).toHaveTextContent("Programmatic holdings writes");
});
