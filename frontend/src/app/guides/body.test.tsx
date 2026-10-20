import { render, cleanup } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, expect, it } from "vitest";
import { catalogs } from "@/locales";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { localizedPath } from "@/lib/seo";
const locales = ["en", "es", "zh-Hans", "zh-Hant"] as const;
afterEach(cleanup);

it.each(locales)("737 A6 %s server-readable sections figures tables and bottom links", async (locale) => {
  const { GuideBody } = await import("./_components/guide-body");
  const { calculateExample } = await import("./_lib/example");
  for (const kind of ["replay", "var"] as const) {
    window.history.replaceState(null, "", localizedPath(`/guides/jade-${kind === 'replay' ? 'replay' : 'var'}`, locale));
    const html = renderToStaticMarkup(<LocaleProvider routeLocale={locale}>
      <GuideBody kind={kind} example={calculateExample()} />
    </LocaleProvider>);
    const doc = new DOMParser().parseFromString(html, "text/html");
    const copy = catalogs[locale].guides;
    expect(doc.querySelector("h1")?.textContent).toBe(copy[kind].title);
    expect(doc.querySelector("main > p")?.classList.contains("text-sm")).toBe(true);
    const paragraphs = doc.querySelectorAll("section > div > p");
    const paragraphCount = Object.values(copy[kind].sections).reduce((count, section) => {
      return count + Object.keys(section).filter(key => key !== "heading").length;
    }, 0);
    expect(paragraphs).toHaveLength(paragraphCount);
    for (const paragraph of paragraphs) expect(paragraph.classList.contains("text-sm")).toBe(true);
    expect(doc.querySelector("nav")?.classList.contains("text-sm")).toBe(true);
    expect(doc.querySelector("nav")?.classList.contains("pt-6")).toBe(true);
    expect([...doc.querySelectorAll("h2")].map(n => n.textContent)).toEqual(Object.values(copy[kind].sections).map(s => s.heading));
    expect(doc.querySelectorAll("table")).toHaveLength(kind === 'replay' ? 1 : 2);
    expect(doc.querySelectorAll("figcaption")).toHaveLength(1);
    for (const n of doc.querySelectorAll("caption,figcaption"))
      expect(n.textContent).toContain(copy.common.exampleLabel);
    expect(doc.querySelector('nav a[href="' + localizedPath('/pricing', locale) + '"]')).not.toBeNull();
    expect(doc.querySelector('nav a[href="' + localizedPath('/guides', locale) + '"]')).not.toBeNull();
    if (locale === "en") {
      expect(doc.querySelector("caption")?.textContent).toContain("60% SPY + 40% AGG");
    }
    if (kind === 'var' && locale === 'en') {
      expect(doc.body.textContent).toContain("On about 95 of every 100 days in the replay the portfolio lost less than 1.14%.");
      expect(doc.body.textContent).toContain("On the other 5 days it lost more, and CVaR (1.66%) is their average loss.");
      expect(doc.querySelector('[data-cell="portfolio-95-daily-var"]')?.textContent).toBe('1.14%$1,144');
    }
  }
});

it.each([["replay", "light"], ["replay", "dark"], ["var", "light"], ["var", "dark"]] as const)("737 A12 375px %s %s tables wrap and charts fill parent", async (kind, theme) => {
  const { GuideBody } = await import('./_components/guide-body');
  const { calculateExample } = await import('./_lib/example');
  const { container } = render(<LocaleProvider routeLocale="en">
    <div className={theme} style={{ width: 375 }}>
      <GuideBody kind={kind} example={calculateExample()} />
    </div>
  </LocaleProvider>);
  for (const table of container.querySelectorAll('table'))
    expect(table).toHaveClass('table-fixed', 'w-full');
  expect(container.querySelector('[data-guide-chart]')).toHaveClass('w-full', 'min-w-0');
  expect(container.querySelector('[class*="overflow-x"]')).toBeNull();
  expect(container.querySelector('.recharts-responsive-container')).not.toBeNull();
});

it("801 A10 figureOnly renders each guide figure without section headings", async () => {
 const { GuideBody } = await import("./_components/guide-body");
 const { Batch2Body } = await import("./_components/batch2-body");
 const { WhatIfGuide } = await import("./_components/what-if-guide");
 const { calculateExample } = await import("./_lib/example");
 const { stressExample, styleExample, trackingExample } = await import("./_lib/example-batch2");
 const { whatIfExample } = await import("./_lib/example-what-if");
 const example = calculateExample();
 for (const figure of [<GuideBody key="replay" kind="replay" example={example} figureOnly />, <GuideBody key="var" kind="var" example={example} figureOnly />, <Batch2Body key="stress" kind="stress" example={stressExample()} figureOnly />, <Batch2Body key="style" kind="style" example={styleExample()} figureOnly />, <Batch2Body key="tracking" kind="tracking" example={trackingExample()} figureOnly />, <WhatIfGuide key="what-if" example={whatIfExample()} figureOnly />]) {
  const { container } = render(<LocaleProvider routeLocale="en">{figure}</LocaleProvider>);
  expect(container.querySelector("figure")).not.toBeNull();
  expect(container.querySelector("h1,h2,section")).toBeNull(); cleanup();
 }
});
