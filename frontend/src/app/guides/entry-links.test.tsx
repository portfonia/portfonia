import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { catalogs, type Locale } from "@/locales";
import type { Me } from "@/lib/api";
import { localizedPath } from "@/lib/seo";
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("@/hooks/use-session", () => ({ revalidateSession: vi.fn() }));
vi.mock("@/app/profile/_components/use-subscription", () => ({ useSubscription: () => ({
  pending: false,
  error: null,
  dialog: null
}) }));
vi.mock("@/app/profile/_components/subscription-dialog", () => ({ SubscriptionDialog: () => null }));
vi.mock("@/app/jade/_components/style-section", () => ({ StyleSection: () => null }));
vi.mock("@/app/jade/_components/stress-section", () => ({ StressSection: () => null }));
vi.mock("@/app/jade/_components/tracking-section", () => ({ TrackingSection: () => null }));
vi.mock("@/lib/api", async () => ({
  ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"),
  getJadeReplay: () => new Promise(() => {
  }),
  getJadeTailRisk: () => new Promise(() => {
  })
}));
import { ReplaySection } from "@/app/jade/_components/replay-section";
import { TailRiskSection } from "@/app/jade/_components/tail-risk-section";
import { JadePageBody } from "@/app/jade/_components/jade-page-body";
import { PlanComparison } from "@/app/pricing/plan-comparison";
afterEach(cleanup);
const me: Me = {
  email: "guide@example.com",
  credit_balance: "10",
  delivery_email: null,
  email_verified_at: "2026-10-01T12:00:00Z",
  delivery_email_verified_at: null,
  tos_accepted_at: null,
  has_questionnaire: true,
  has_holdings: false,
  missing: [],
  pending_email_verifications: [],
  report_language: "en",
  report_currency: "USD",
  subscription: {
    status: "inactive",
    type: null,
    cadence: "none",
    expires_on: null,
    cancel_pending: false,
    next_adjustment_at: null
  }
};

it.each(["en", "es", "zh-Hans", "zh-Hant"] as const)("737 A9 %s card links outside inert overlays", locale => {
  for (const [node, path] of [[<ReplaySection key="replay" />, "/guides/jade-replay"], [<TailRiskSection key="tail" settings={null} />, "/guides/jade-var"]] as const) {
    render(<LocaleProvider routeLocale={locale}>{node}</LocaleProvider>);
    const link = screen.getByRole('link', { name: catalogs[locale].jade.guideLink });
    expect(link).toHaveAttribute('href', localizedPath(path, locale));
    expect(link.closest('[inert]')).toBeNull();
    const copy = path === '/guides/jade-replay' ? catalogs[locale].jade.replay : catalogs[locale].jade.tailRisk;
    const explanation = screen.getByText(copy.whatItSays);
    expect(screen.getByText(copy.intro).nextElementSibling).toBe(explanation);
    expect(explanation.nextElementSibling).toBe(link);
    cleanup();
  }
});
const introCases = (["en", "es", "zh-Hans", "zh-Hant"] as const).flatMap(locale => {
  return [false, true].map(active => ({ locale, active }));
});


it.each(introCases)("737 A9 intro links with $locale Jade subscription $active", ({ locale, active }) => {
  const subscriber: Me = active ? {
    ...me,
    subscription: { ...me.subscription, status: "active", type: "jade", cadence: "weekly" },
  } : me;
  render(
    <LocaleProvider routeLocale={locale}>
      <JadePageBody me={subscriber} hadLoadError={false} />
    </LocaleProvider>
  );
  const copy = catalogs[locale].jade;
  const intro = screen.getByText((_, element) => element?.tagName === "P" && !!element.textContent?.startsWith(copy.guidesLead));
  const indexLink = within(intro).getByRole("link", { name: copy.guideLinks.index });
  expect(indexLink).toHaveAttribute("href", localizedPath("/guides", locale));
  const leadSeparator = { en: " ", es: " ", "zh-Hans": "", "zh-Hant": "" }[locale];
  expect(indexLink.parentElement?.querySelectorAll("a")).toHaveLength(1);
  expect(indexLink.parentElement?.textContent).toBe(
    `${copy.guidesLead}${leadSeparator}${copy.guideLinks.index}`
  );
});

it.each(["en", "es", "zh-Hans", "zh-Hant"] as Locale[])("737 A9 %s Pricing only Jade feature indices one through five link", locale => {
  const { container } = render(<LocaleProvider routeLocale={locale}>
    <PlanComparison />
  </LocaleProvider>);
  const plans = container.querySelectorAll('[data-plan]');
  plans.forEach((plan, i) => {
    expect([...plan.querySelectorAll('li')].map(li => li.textContent)).toEqual(catalogs[locale].pricingPlans.plans[i].features);
    expect(plan.querySelectorAll('li a')).toHaveLength(i === 2 ? 5 : 0);
  });
  const jade = within(plans[2] as HTMLElement);
  expect(jade.getByRole('link', { name: catalogs[locale].pricingPlans.plans[2].features[1] })).toHaveAttribute('href', localizedPath('/guides/jade-replay', locale));
  expect(jade.getByRole('link', { name: catalogs[locale].pricingPlans.plans[2].features[2] })).toHaveAttribute('href', localizedPath('/guides/jade-var', locale));
  expect(jade.getByRole('link', { name: catalogs[locale].pricingPlans.plans[2].features[3] })).toHaveAttribute('href', localizedPath('/guides/jade-stress', locale));
  expect(jade.getByRole('link', { name: catalogs[locale].pricingPlans.plans[2].features[4] })).toHaveAttribute('href', localizedPath('/guides/jade-style', locale));
  expect(jade.getByRole('link', { name: catalogs[locale].pricingPlans.plans[2].features[5] })).toHaveAttribute('href', localizedPath('/guides/jade-tracking', locale));
});
