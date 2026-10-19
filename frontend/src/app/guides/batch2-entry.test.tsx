import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { catalogs } from "@/locales";
import { localizedPath } from "@/lib/seo";
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), getJadeStress: () => new Promise(() => {}), getJadeStyle: () => new Promise(() => {}), getJadeTracking: () => new Promise(() => {}) }));
import { StressSection } from "@/app/jade/_components/stress-section";
import { StyleSection } from "@/app/jade/_components/style-section";
import { TrackingSection } from "@/app/jade/_components/tracking-section";
import { PlanComparison } from "@/app/pricing/plan-comparison";
afterEach(cleanup);
it.each(["en", "es", "zh-Hans", "zh-Hant"] as const)("739 A11 %s three card links stay outside overlay and follow explanation", locale => {
  for (const [kind, node] of [["stress", <StressSection key="stress" settings={null} />], ["style", <StyleSection key="style" settings={null} />], ["tracking", <TrackingSection key="tracking" settings={null} />]] as const) {
    render(<LocaleProvider routeLocale={locale}>{node}</LocaleProvider>);
    const link = screen.getByRole("link", { name: catalogs[locale].jade.guideLink });
    expect(link).toHaveAttribute("href", localizedPath(`/guides/jade-${kind}`, locale));
    expect(link.closest("[inert], [aria-busy]")).toBeNull();
    expect(screen.getByText(catalogs[locale].jade[kind].whatItSays).nextElementSibling).toBe(link); cleanup();
  }
});
it.each(["en", "es", "zh-Hans", "zh-Hant"] as const)("739 A11 %s Pricing has seven ordered Jade links with original text", locale => {
  const { container } = render(<LocaleProvider routeLocale={locale}><PlanComparison /></LocaleProvider>);
  expect([...container.querySelectorAll('[data-plan="jade"] li a')].map(e => e.getAttribute("href"))).toEqual([...(["replay", "var", "stress", "style", "tracking", "what-if"].map(k => localizedPath(`/guides/jade-${k}`, locale))), localizedPath("/agent/guide", locale)]);
  for (const [i, plan] of [...container.querySelectorAll('[data-plan]')].entries()) expect([...plan.querySelectorAll('li')].map(e => e.textContent)).toEqual(catalogs[locale].pricingPlans.plans[i].features.map(s => s.replace(/<\/?[a-z]+>/g, "")));
  expect(container.querySelectorAll('[data-plan="briefing"] a')).toHaveLength(0);
  expect(container.querySelectorAll('[data-plan="advanced"] a')).toHaveLength(0);
});
