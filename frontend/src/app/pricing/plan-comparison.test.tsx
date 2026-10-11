import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { LegalDocument } from "@/app/_components/legal-document";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { catalogs, type Locale } from "@/locales";
import PricingPage from "./page";
import { localizedPath } from "@/lib/seo";

// Exact Design D2/D5 and owner-approved copy; Unicode escapes keep this test English-only.
const expected = {
  "en": {
    "pricingPlans": {
      "heading": "Plans at a glance",
      "plans": [
        {
          "name": "Briefing",
          "prices": [
            "Weekly \u2014 0.99 credits / month",
            "Mon / Wed / Fri \u2014 1.99 credits / month"
          ],
          "lead": "",
          "features": [
            "A personalized briefing email every Saturday, or every Monday, Wednesday and Friday",
            "Holdings upload from CSV, Excel or Markdown, with row-level editing",
            "Portfolio overview: allocation, performance against benchmarks, volatility and risk views",
            "Report history on the web, with Markdown or PDF download",
            "Your AI agent can read your report history"
          ]
        },
        {
          "name": "Advanced",
          "prices": [
            "2.49 credits / month"
          ],
          "lead": "Everything in Briefing, plus:",
          "features": [
            "A briefing every weekday, Monday to Friday",
            "Your AI agent can read your daily holding snapshots",
            "Your AI agent can read intelligence related to your holdings"
          ]
        },
        {
          "name": "Jade",
          "prices": [
            "9.99 credits / month"
          ],
          "lead": "Everything in Advanced, plus:",
          "features": [
            "Choose weekly, Mon / Wed / Fri or daily briefings, and change at any time",
            "Holdings replay: today's holdings over up to five years",
            "Tail risk (VaR / CVaR)",
            "Historical stress scenarios: five past market shocks",
            "Style exposure",
            "Tracking against your reference: a portfolio you define, with tracking error"
          ]
        }
      ],
      "footnote": "1 credit = US$1."
    },
    "jadeBody": "Jade \u2014 9.99 credits per month. Includes everything in the Advanced plan. You choose the Weekly, Mon / Wed / Fri, or Daily briefing schedule and can change it at any time on the Jade page. Jade includes five portfolio risk tools: holdings replay, tail risk (VaR / CVaR), historical stress scenarios, style exposure, and tracking against your reference.",
    "lastUpdated": "Last updated: 2026-10-11",
    "description": "Weekly, Mon/Wed/Fri, or weekday daily briefings, and Jade portfolio risk tools, paid with prepaid credits from US$10. Unused purchased credits are refundable within 120 days."
  },
  "zh-Hans": {
    "pricingPlans": {
      "heading": "\u5957\u9910\u4e00\u89c8",
      "plans": [
        {
          "name": "\u7b80\u62a5",
          "prices": [
            "\u6bcf\u5468 \u2014 \u6bcf\u6708 0.99 credits",
            "\u9694\u65e5\uff08\u5468\u4e00\u3001\u5468\u4e09\u3001\u5468\u4e94\uff09\u2014 \u6bcf\u6708 1.99 credits"
          ],
          "lead": "",
          "features": [
            "\u6bcf\u5468\u516d\u4e00\u5c01\u4e2a\u6027\u5316\u7b80\u62a5\u90ae\u4ef6\uff0c\u6216\u6bcf\u5468\u4e00\u3001\u5468\u4e09\u3001\u5468\u4e94\u5404\u4e00\u5c01",
            "\u901a\u8fc7 CSV\u3001Excel \u6216 Markdown \u4e0a\u4f20\u6301\u4ed3\uff0c\u53ef\u9010\u884c\u7f16\u8f91",
            "\u6295\u8d44\u7ec4\u5408\u6982\u89c8\uff1a\u8d44\u4ea7\u914d\u7f6e\u3001\u76f8\u5bf9\u57fa\u51c6\u7684\u8868\u73b0\u3001\u6ce2\u52a8\u4e0e\u98ce\u9669\u89c6\u56fe",
            "\u7f51\u9875\u67e5\u770b\u5386\u53f2\u62a5\u544a\uff0c\u53ef\u4e0b\u8f7d Markdown \u6216 PDF",
            "AI\u667a\u80fd\u4f53\u53ef\u8bfb\u53d6\u60a8\u7684\u5386\u53f2\u62a5\u544a"
          ]
        },
        {
          "name": "\u8fdb\u9636",
          "prices": [
            "\u6bcf\u6708 2.49 credits"
          ],
          "lead": "\u5305\u542b\u7b80\u62a5\u7684\u5168\u90e8\u529f\u80fd\uff0c\u53e6\u52a0\uff1a",
          "features": [
            "\u6bcf\u4e2a\u5de5\u4f5c\u65e5\uff08\u5468\u4e00\u81f3\u5468\u4e94\uff09\u4e00\u5c01\u7b80\u62a5",
            "AI\u667a\u80fd\u4f53\u53ef\u8bfb\u53d6\u60a8\u7684\u9010\u65e5\u6301\u4ed3\u5feb\u7167",
            "AI\u667a\u80fd\u4f53\u53ef\u8bfb\u53d6\u4e0e\u60a8\u6301\u4ed3\u76f8\u5173\u7684\u60c5\u62a5"
          ]
        },
        {
          "name": "\u6da6\u7389",
          "prices": [
            "\u6bcf\u6708 9.99 credits"
          ],
          "lead": "\u5305\u542b\u8fdb\u9636\u7684\u5168\u90e8\u529f\u80fd\uff0c\u53e6\u52a0\uff1a",
          "features": [
            "\u7b80\u62a5\u9891\u7387\u53ef\u5728\u6bcf\u5468\u3001\u9694\u65e5\u3001\u6bcf\u65e5\u4e4b\u95f4\u81ea\u9009\uff0c\u968f\u65f6\u8c03\u6574",
            "\u6301\u4ed3\u56de\u653e\uff1a\u628a\u4eca\u5929\u7684\u6301\u4ed3\u653e\u56de\u8fc7\u53bb\u6700\u957f\u4e94\u5e74",
            "\u5c3e\u90e8\u98ce\u9669\uff08VaR / CVaR\uff09",
            "\u5386\u53f2\u60c5\u666f\u538b\u529b\u6d4b\u8bd5\uff1a\u4e94\u6b21\u5386\u53f2\u5e02\u573a\u51b2\u51fb",
            "\u98ce\u683c\u655e\u53e3",
            "\u4e0e\u81ea\u5b9a\u53c2\u7167\u7684\u8ddf\u8e2a\u6bd4\u8f83\uff1a\u81ea\u5df1\u5b9a\u4e49\u53c2\u7167\u7ec4\u5408\uff0c\u8ba1\u7b97\u8ddf\u8e2a\u8bef\u5dee"
          ]
        }
      ],
      "footnote": "1 credit = 1 \u7f8e\u5143\u3002"
    },
    "jadeBody": "\u6da6\u7389\u2014\u2014\u6bcf\u6708 9.99 credits\u3002\u5305\u542b\u8fdb\u9636\u5957\u9910\u7684\u5168\u90e8\u529f\u80fd\u3002\u60a8\u53ef\u9009\u62e9\u6bcf\u5468\u3001\u9694\u65e5\uff08\u5468\u4e00\u3001\u5468\u4e09\u3001\u5468\u4e94\uff09\u6216\u6bcf\u65e5\u7b80\u62a5\uff0c\u5e76\u968f\u65f6\u5728\u6da6\u7389\u5e73\u53f0\u8c03\u6574\u3002\u6da6\u7389\u5305\u542b\u4e94\u9879\u6295\u8d44\u7ec4\u5408\u98ce\u9669\u5de5\u5177\uff1a\u6301\u4ed3\u56de\u653e\u3001\u5c3e\u90e8\u98ce\u9669\uff08VaR / CVaR\uff09\u3001\u5386\u53f2\u60c5\u666f\u538b\u529b\u6d4b\u8bd5\u3001\u98ce\u683c\u655e\u53e3\u3001\u4e0e\u81ea\u5b9a\u53c2\u7167\u7684\u8ddf\u8e2a\u6bd4\u8f83\u3002",
    "lastUpdated": "\u6700\u540e\u66f4\u65b0\uff1a2026-10-11",
    "description": "\u6bcf\u5468\u3001\u9694\u65e5\uff08\u5468\u4e00\u3001\u5468\u4e09\u3001\u5468\u4e94\uff09\u6216\u5de5\u4f5c\u65e5\u6bcf\u65e5\u7b80\u62a5\uff0c\u4ee5\u53ca\u6da6\u7389\u7ec4\u5408\u98ce\u9669\u5de5\u5177\uff0c\u4ee5 US$10 \u8d77\u7684\u9884\u4ed8\u670d\u52a1\u70b9\u6570\u652f\u4ed8\u3002\u8d2d\u4e70\u540e 120 \u5929\u5185\uff0c\u672a\u4f7f\u7528\u7684\u5df2\u8d2d\u70b9\u6570\u53ef\u7533\u8bf7\u9000\u6b3e\u3002"
  },
  "zh-Hant": {
    "pricingPlans": {
      "heading": "\u65b9\u6848\u4e00\u89bd",
      "plans": [
        {
          "name": "\u7c21\u5831",
          "prices": [
            "\u6bcf\u9031 \u2014 \u6bcf\u6708 0.99 credits",
            "\u9694\u65e5\uff08\u9031\u4e00\u3001\u9031\u4e09\u3001\u9031\u4e94\uff09\u2014 \u6bcf\u6708 1.99 credits"
          ],
          "lead": "",
          "features": [
            "\u6bcf\u9031\u516d\u4e00\u5c01\u500b\u4eba\u5316\u7c21\u5831\u90f5\u4ef6\uff0c\u6216\u6bcf\u9031\u4e00\u3001\u9031\u4e09\u3001\u9031\u4e94\u5404\u4e00\u5c01",
            "\u900f\u904e CSV\u3001Excel \u6216 Markdown \u4e0a\u50b3\u6301\u5009\uff0c\u53ef\u9010\u5217\u7de8\u8f2f",
            "\u6295\u8cc7\u7d44\u5408\u6982\u89bd\uff1a\u8cc7\u7522\u914d\u7f6e\u3001\u76f8\u5c0d\u57fa\u6e96\u7684\u8868\u73fe\u3001\u6ce2\u52d5\u8207\u98a8\u96aa\u6aa2\u8996",
            "\u7db2\u9801\u6aa2\u8996\u6b77\u53f2\u5831\u544a\uff0c\u53ef\u4e0b\u8f09 Markdown \u6216 PDF",
            "AI \u667a\u80fd\u9ad4\u53ef\u8b80\u53d6\u60a8\u7684\u6b77\u53f2\u5831\u544a"
          ]
        },
        {
          "name": "\u9032\u968e",
          "prices": [
            "\u6bcf\u6708 2.49 credits"
          ],
          "lead": "\u6db5\u84cb\u7c21\u5831\u7684\u6240\u6709\u529f\u80fd\uff0c\u53e6\u52a0\uff1a",
          "features": [
            "\u6bcf\u500b\u5de5\u4f5c\u65e5\uff08\u9031\u4e00\u81f3\u9031\u4e94\uff09\u4e00\u5c01\u7c21\u5831",
            "AI \u667a\u80fd\u9ad4\u53ef\u8b80\u53d6\u60a8\u7684\u9010\u65e5\u6301\u5009\u5feb\u7167",
            "AI \u667a\u80fd\u9ad4\u53ef\u8b80\u53d6\u8207\u60a8\u6301\u5009\u76f8\u95dc\u7684\u60c5\u5831"
          ]
        },
        {
          "name": "\u6f64\u7389",
          "prices": [
            "\u6bcf\u6708 9.99 credits"
          ],
          "lead": "\u6db5\u84cb\u9032\u968e\u7684\u6240\u6709\u529f\u80fd\uff0c\u53e6\u52a0\uff1a",
          "features": [
            "\u7c21\u5831\u983b\u7387\u53ef\u5728\u6bcf\u9031\u3001\u9694\u65e5\u3001\u6bcf\u65e5\u4e4b\u9593\u81ea\u9078\uff0c\u96a8\u6642\u8abf\u6574",
            "\u6301\u5009\u56de\u653e\uff1a\u628a\u4eca\u5929\u7684\u6301\u5009\u653e\u56de\u904e\u53bb\u6700\u9577\u4e94\u5e74",
            "\u5c3e\u90e8\u98a8\u96aa\uff08VaR / CVaR\uff09",
            "\u6b77\u53f2\u60c5\u5883\u58d3\u529b\u6e2c\u8a66\uff1a\u4e94\u6b21\u6b77\u53f2\u5e02\u5834\u885d\u64ca",
            "\u98a8\u683c\u66dd\u96aa",
            "\u8207\u81ea\u8a02\u53c3\u7167\u7684\u8ffd\u8e64\u6bd4\u8f03\uff1a\u81ea\u884c\u5b9a\u7fa9\u53c3\u7167\u7d44\u5408\uff0c\u8a08\u7b97\u8ffd\u8e64\u8aa4\u5dee"
          ]
        }
      ],
      "footnote": "1 credit = 1 \u7f8e\u5143\u3002"
    },
    "jadeBody": "\u6f64\u7389\u2014\u2014\u6bcf\u6708 9.99 credits\u3002\u6db5\u84cb\u9032\u968e\u65b9\u6848\u7684\u6240\u6709\u529f\u80fd\u3002\u60a8\u53ef\u9078\u64c7\u6bcf\u9031\u3001\u9694\u65e5\uff08\u9031\u4e00\u3001\u9031\u4e09\u3001\u9031\u4e94\uff09\u6216\u6bcf\u65e5\u7c21\u5831\uff0c\u4e26\u96a8\u6642\u81f3\u6f64\u7389\u5e73\u53f0\u8b8a\u66f4\u3002\u6f64\u7389\u5305\u542b\u4e94\u9805\u6295\u8cc7\u7d44\u5408\u98a8\u96aa\u5de5\u5177\uff1a\u6301\u5009\u56de\u653e\u3001\u5c3e\u90e8\u98a8\u96aa\uff08VaR / CVaR\uff09\u3001\u6b77\u53f2\u60c5\u5883\u58d3\u529b\u6e2c\u8a66\u3001\u98a8\u683c\u66dd\u96aa\u3001\u8207\u81ea\u8a02\u53c3\u7167\u7684\u8ffd\u8e64\u6bd4\u8f03\u3002",
    "lastUpdated": "\u6700\u5f8c\u66f4\u65b0\uff1a2026-10-11",
    "description": "\u6bcf\u9031\u3001\u9694\u65e5\uff08\u9031\u4e00\u3001\u9031\u4e09\u3001\u9031\u4e94\uff09\u6216\u5de5\u4f5c\u65e5\u6bcf\u65e5\u7c21\u5831\uff0c\u4ee5\u53ca\u6f64\u7389\u7d44\u5408\u98a8\u96aa\u5de5\u5177\uff0c\u4ee5 US$10 \u8d77\u7684\u9810\u4ed8\u670d\u52d9\u9ede\u6578\u652f\u4ed8\u3002\u8cfc\u8cb7\u5f8c 120 \u5929\u5167\uff0c\u672a\u4f7f\u7528\u7684\u5df2\u8cfc\u9ede\u6578\u53ef\u7533\u8acb\u9000\u6b3e\u3002"
  }
} as const;

function renderPricing(locale: Locale = "en") {
  window.history.replaceState(null, "", locale === "en" ? "/pricing" : `/${locale}/pricing`);
  render(<LocaleProvider routeLocale={locale === "en" ? null : locale}><PricingPage /></LocaleProvider>);
}

afterEach(() => window.history.replaceState(null, "", "/"));

function comparison() {
  return screen.getByRole("region", { name: expected.en.pricingPlans.heading });
}

it("A1 comparison follows the intro and precedes section one", () => {
  renderPricing();
  const intro = screen.getByText(catalogs.en.legal.pricing.intro);
  const heading = screen.getByRole("heading", { level: 2, name: expected.en.pricingPlans.heading });
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
    expect(within(card).getAllByRole("listitem").map(li => li.textContent)).toEqual(plan.features);
    expect(within(card).getAllByRole("listitem")).toHaveLength([5, 3, 6][i]);
    expect(Array.from(card.querySelectorAll("p")).map(p => p.textContent)).toEqual([...plan.prices, ...(plan.lead ? [plan.lead] : [])]);
  });
  expect(catalogs.en.pricingPlans).toEqual(expected.en.pricingPlans);
});

it.each(["zh-Hans", "zh-Hant"] as const)("A3 %s renders exact owner-approved names and Jade features", locale => {
  renderPricing(locale);
  const block = screen.getByRole("region", { name: expected[locale].pricingPlans.heading });
  const headings = within(block).getAllByRole("heading", { level: 3 });
  expect(headings.map(h => h.textContent)).toEqual(expected[locale].pricingPlans.plans.map(plan => plan.name));
  const jade = within(block).getAllByRole("region")[2];
  expect(within(jade).getAllByRole("listitem").map(li => li.textContent)).toEqual(expected[locale].pricingPlans.plans[2].features);
  expect(catalogs[locale].pricingPlans).toEqual(expected[locale].pricingPlans);
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
  expect(within(comparison()).getByText(expected.en.pricingPlans.footnote)).toBeInTheDocument();
});

it.each(["en", "es", "zh-Hans", "zh-Hant"] as const)("A6 %s comparison contains no buttons and exactly two Jade guide links", locale => {
  renderPricing(locale);
  const block = screen.getByRole("region", { name: catalogs[locale].pricingPlans.heading });
  expect(block.querySelectorAll("button")).toHaveLength(0);
  const links = Array.from(block.querySelectorAll("a"));
  expect(links).toHaveLength(2);
  const jade = block.querySelector('[data-plan="jade"]');
  for (const link of links) expect(jade).toContainElement(link);
  expect(links.map(link => link.getAttribute("href"))).toEqual([
    localizedPath("/guides/jade-replay", locale),
    localizedPath("/guides/jade-var", locale),
  ]);
});

it("A7 cards stack below the three-column medium breakpoint", () => {
  renderPricing();
  const card = within(comparison()).getAllByRole("region")[0];
  expect(card.parentElement).toHaveClass("grid", "grid-cols-1", "md:grid-cols-3");
});

it.each(["en", "zh-Hans", "zh-Hant"] as const)("A8 %s exact Pricing copy and non-advisory plan strings", locale => {
  const c = catalogs[locale];
  expect(c.legal.pricing.sections[0].body[3]).toBe(expected[locale].jadeBody);
  expect(c.legal.pricing.lastUpdated).toBe(expected[locale].lastUpdated);
  expect(c.seo.pages.pricing.description).toBe(expected[locale].description);
  expect(c.pricingPlans).toEqual(expected[locale].pricingPlans);
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
