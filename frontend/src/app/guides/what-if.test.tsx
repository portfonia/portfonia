import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { catalogs } from "@/locales";
import { localizedPath, buildPageMetadata } from "@/lib/seo";
import sitemap from "@/app/sitemap";
import { WhatIfGuide } from "./_components/what-if-guide";
import { GuidesIndex } from "./_components/guides-index";
import { PlanComparison } from "@/app/pricing/plan-comparison";
import { whatIfExample } from "./_lib/example-what-if";
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
afterEach(cleanup);
it("F7 four locale guide, six cards, sitemap and ordered Pricing link", () => {
 for (const locale of ["en","es","zh-Hans","zh-Hant"] as const) {
  const {container}=render(<LocaleProvider routeLocale={locale}><WhatIfGuide example={whatIfExample()} /><GuidesIndex /><PlanComparison /></LocaleProvider>);
  expect(container.textContent).toContain(catalogs[locale].guides.whatIf.title);
  expect(container.querySelectorAll('[data-guide-card]')).toHaveLength(6);
  expect(container.querySelector('[data-guide-card]:last-child a')).toHaveAttribute('href',localizedPath('/guides/jade-what-if',locale));
  const rows=[...container.querySelectorAll('[data-plan="jade"] li')];
  const index=rows.findIndex(row=>row.querySelector(`a[href="${localizedPath('/guides/jade-what-if',locale)}"]`));
  expect(index).toBeGreaterThan(0); expect(index).toBe(rows.findIndex(row=>row.querySelector(`a[href="${localizedPath('/guides/jade-tracking',locale)}"]`))+1);
  expect(sitemap().some(row=>row.url===`https://portfonia.com${localizedPath('/guides/jade-what-if',locale)}`)).toBe(true);
  expect(buildPageMetadata('guideWhatIf',locale).title).toBe(catalogs[locale].seo.pages.guideWhatIf.title); cleanup();
 }
});
