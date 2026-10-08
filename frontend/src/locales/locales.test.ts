import { describe, expect, it } from "vitest";

import { catalogs, type Locale } from "./index";

// Arrays hold raw sample/structured data (home.preview.holdingsRows etc.),
// not per-key translated strings — comparing their contents across locales
// isn't meaningful (row count/order legitimately differs from column
// headers), so an array is treated as one leaf at its own path rather than
// recursed into.
function leafPaths(value: unknown, prefix = ""): string[] {
  if (Array.isArray(value)) return [prefix];
  if (value !== null && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).flatMap(([key, v]) =>
      leafPaths(v, prefix ? `${prefix}.${key}` : key),
    );
  }
  return [prefix];
}

// Every catalog-backed locale, not just LOCALES (the switcher-exposed
// subset) — this test must keep covering a locale pending human review
// (e.g. zh-Hant) so its shape can't silently drift while it's excluded from
// the switcher (blacktomb42 review, PR #226).
const LOCALE_VALUES = Object.keys(catalogs) as Locale[];

describe("locale catalogs stay structurally in sync (issue #209)", () => {
  const shapes = Object.fromEntries(
    LOCALE_VALUES.map((locale) => [locale, new Set(leafPaths(catalogs[locale]))]),
  );

  it.each(LOCALE_VALUES)(
    "%s has no key paths missing from any other locale (adding a 4th locale must not silently drop content)",
    (locale) => {
      for (const other of LOCALE_VALUES) {
        if (other === locale) continue;
        const missingFromOther = [...shapes[locale]].filter((p) => !shapes[other].has(p));
        expect(missingFromOther, `${locale} has keys missing from ${other}`).toEqual([]);
      }
    },
  );

  it("every locale carries exactly the expected top-level namespaces", () => {
    const expected = [
      "about",
      "agent",
      "auth",
      "common",
      "downloadConfirm",
      "emailVerification",
      "holdings",
      "home",
      "legal",
      "menu",
      "notFound",
      "portfolio",
      "profile",
      "questionnaire",
      "reports",
      "seo",
      "unsubscribe",
      "welcome",
    ].sort();
    for (const locale of LOCALE_VALUES) {
      expect(Object.keys(catalogs[locale]).sort()).toEqual(expected);
    }
  });
});

describe("in-place credit purchase copy", () => {
  it("replaces the redirect notice in every locale", () => {
    expect(catalogs.en.profile.creditPurchaseOpening).toBe("Opening checkout…");
    expect(catalogs.en.profile.creditPurchaseOpenFailed).toBe(
      "Checkout did not open. Please try again.",
    );
    expect(catalogs.en.profile.creditPurchasePending).toBe(
      "Payment received. Adding your credits; this usually takes under a minute.",
    );
    expect(catalogs.en.profile.creditPurchaseCredited).toBe(
      "{credits} credits added to your balance.",
    );
    expect(catalogs.en.profile.creditPurchaseDelayed).toBe(
      "Your payment is complete. Credits usually appear within a few minutes; if they are not shown after 10 minutes, contact info@portfonia.com.",
    );
    for (const locale of LOCALE_VALUES) {
      expect(catalogs[locale].profile).not.toHaveProperty("creditPurchaseCompleted");
    }
  });
});

describe("legal copy for Paddle review", () => {
  it.each(LOCALE_VALUES)("%s contains no superseded names, placeholders, or status copy", (locale) => {
    expect(JSON.stringify(catalogs[locale])).not.toMatch(/Portfonia LLC|Portfonia AI|\{\{|closed beta|legal review/i);
  });

  it.each(LOCALE_VALUES)("%s names the merchant of record only via placeholders and the reseller notice", (locale) => {
    const { resellerNotice, ...documents } = catalogs[locale].legal;
    const serialized = JSON.stringify(documents);
    expect(serialized).not.toMatch(/Paddle/);
    expect(serialized).toContain("{merchantOfRecord}");
    expect(serialized).toContain("{resellerNotice}");
    expect(resellerNotice).toMatch(/Paddle/);
  });

  it("keeps Paddle's required English reseller notice verbatim", () => {
    expect(catalogs.en.legal.resellerNotice).toBe(
      "Our order process is conducted by our online reseller Paddle.com. Paddle.com is the Merchant of Record for all our orders. Paddle provides all customer service inquiries and handles returns.",
    );
  });

  it.each(LOCALE_VALUES)("%s Privacy retention does not promise deletion of all associated data", (locale) => {
    expect(catalogs[locale].legal.privacy.sections[5].body[0]).not.toMatch(/full deletion|彻底删除|徹底刪除/);
  });

  it("keeps the required Terms and Privacy section structure", () => {
    expect(catalogs.en.legal.terms.sections).toHaveLength(13);
    expect(catalogs.en.legal.terms.sections[3].heading).toBe("4. Payments and Credits");
    expect(catalogs.en.legal.privacy.sections).toHaveLength(13);
  });
});


describe("subscription public billing copy (#597)", () => {
  it("distinguishes changes from cancellation and documents limits and notices", () => {
    const change = "If you change your plan, the unused part of the current month's fee is returned to your credit balance pro rata and the new plan starts that day. If you cancel, your plan stays active until the end of the period already paid and nothing is returned.";
    expect(catalogs.en.legal.pricing.sections[3].body[2]).toBe(change);
    expect(catalogs.en.legal.terms.sections[3].body[1]).toBe("Your plan fee is deducted from your credit balance at the start of each monthly billing period, complimentary credits first. " + change);
    expect(catalogs.en.legal.refund.sections[1].body[1]).toBe("Credits already applied to a billing period are not refunded to your payment method. When you change a plan, the unused part of the current month's fee is returned to your credit balance pro rata. When you cancel, nothing is returned for the remaining days.");
    expect(catalogs.en.legal.terms.sections[3].body).toContain("You can adjust your subscription (subscribe, change plan, cancel, or resume) once per day, Eastern Time.");
    expect(catalogs.en.legal.terms.sections[3].body).toContain("If you unsubscribe an address from report email and no verified address remains on your account, your subscription is treated as cancelled: it stays active until the end of the period already paid, nothing is returned, and it does not resume when you verify an address again.");
    expect(catalogs.en.legal.terms.sections[3].body).toContain("Balance reminders and expiry notices are sent only to a verified address.");
  });
  it.each(LOCALE_VALUES)("%s removes schedule placeholders and preserves matching billing structure", locale => {
    expect(catalogs[locale].profile).not.toHaveProperty("reportSchedulePlaceholder");
    expect(Object.keys(catalogs[locale].profile.reportScheduleOptions)).toEqual(["weekly", "everyOtherDay", "daily"]);
    expect(catalogs[locale].welcome).not.toHaveProperty("cadence");
    expect(catalogs[locale].legal.terms.sections[3].body).toHaveLength(8);
  });
});

it("acceptance_20 all report keys exist in every catalog", () => {
  const keys = ["title", "category", "sort", "newest", "oldest", "dateFrom", "dateTo", "previous", "next", "page", "empty", "emptyFiltered", "underReview", "generating", "downloadMd", "print", "loadError", "types"];
  for (const locale of LOCALE_VALUES) {
    expect(catalogs[locale].menu).toHaveProperty("reports");
    expect(catalogs[locale]).toHaveProperty("reports");
    for (const key of keys) expect(catalogs[locale]).toHaveProperty(`reports.${key}`);
  }
});

it("acceptance_16 agent copy and legal sections match #651", () => {
  for (const locale of LOCALE_VALUES) {
    expect(catalogs[locale]).toHaveProperty("agent");
    expect(catalogs[locale].menu).toHaveProperty("agent");
    expect(catalogs[locale].legal.privacy.sections).toHaveLength(13);
    expect(catalogs[locale].legal.privacy.sections.map(section => section.heading.split(".")[0])).toEqual(Array.from({ length: 13 }, (_, i) => String(i + 1)));
    expect(catalogs[locale].legal.terms.sections).toHaveLength(13);
    expect(catalogs[locale].legal.terms.sections[5].body).toHaveLength(2);
  }
  expect(catalogs.en.legal.privacy.sections[6].heading).toBe("7. AI Agent access");
  expect(catalogs["zh-Hans"].menu.agent).toBe("\u0041\u0049\u667a\u80fd\u4f53");
  expect(catalogs["zh-Hant"].menu.agent).toBe("AI \u667a\u80fd\u9ad4");
});


describe("issue #652 agent documentation catalogs", () => {
  it.each(LOCALE_VALUES)("acceptance_09 %s carries only the new getting-started and endpoint keys", locale => {
    const agent = catalogs[locale].agent;
    expect(Object.keys(agent.docs).sort()).toEqual(["gettingStartedTitle", "gettingStarted", "endpointsTitle", "reports", "snapshots", "intel"].sort());
    for (const copy of Object.values(agent.docs)) {
      expect(copy.trim().length).toBeGreaterThan(0);
      expect([agent.intro, agent.limitsTitle, agent.limits, agent.quiet, agent.notice, agent.tokenRule]).not.toContain(copy);
    }
  });
});


it.each(LOCALE_VALUES)("%s referral copy and Terms preserve the required layout (#675)", locale => {
  const catalog = catalogs[locale];
  expect(catalog.profile).not.toHaveProperty("invitePlaceholder");
  for (const key of ["referralThanks", "referralEmailRequired", "referralDailyLimit", "referralError", "deleteAccountNegativeBalance"]) {
    expect(catalog.profile).toHaveProperty(key);
  }
  expect(catalog.profile.inviteBody).not.toMatch(/\d|%/);
  expect(catalog.profile.deleteAccountNegativeBalance).toContain("info@portfonia.com");
  const expectedUpdated = locale === "en" ? "Last updated: 2026-10-05"
    : locale === "zh-Hans" ? "\u6700\u540e\u66f4\u65b0\uff1a2026-10-05"
      : "\u6700\u5f8c\u66f4\u65b0\uff1a2026-10-05";
  expect(catalog.legal.terms.lastUpdated).toBe(expectedUpdated);
  expect(catalog.legal.terms.sections[3].body[6]).not.toMatch(/\d|%/);
  expect(catalog.legal.terms.sections[3].body[7]).toContain("{resellerNotice}");
});
