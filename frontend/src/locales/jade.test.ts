import { expect, it } from "vitest";
import { catalogs } from "./index";

it.each(["zh-Hans", "zh-Hant"] as const)("A11 %s renames only Profile page references", locale => {
  const catalog = catalogs[locale];
  const old = locale === "zh-Hans" ? "\u4e2a\u4eba\u8d44\u6599" : "\u500b\u4eba\u8cc7\u6599";
  const name = locale === "zh-Hans" ? "\u4e2a\u4eba\u4e2d\u5fc3" : "\u500b\u4eba\u4e2d\u5fc3";
  expect(catalog.menu.profile).toBe(name);
  expect(catalog.profile.pageTitle).toBe(name);
  expect(catalog.profile.backToProfile).toContain(name);
  expect(catalog.unsubscribe.subtitle).toContain(name);
  expect(catalog.legal.privacy.sections[7].body[0]).toContain(name);
  const remaining: string[] = [];
  function inspect(value: unknown, path: string) {
    if (typeof value === "string" && value.includes(old)) remaining.push(path);
    else if (value && typeof value === "object") for (const [key, child] of Object.entries(value)) inspect(child, `${path}.${key}`);
  }
  inspect(catalog, "");
  expect(remaining).toEqual(locale === "zh-Hans" ? [".legal.privacy.sections.7.body.0"] : [".legal.privacy.sections.5.body.1", ".legal.privacy.sections.7.body.0", ".legal.privacy.sections.10.body.0"]);
  expect(catalog.legal.privacy.sections[7].body[0].split(old)).toHaveLength(2);
});

it.each(["en", "zh-Hans", "zh-Hant"] as const)("D9 %s includes the Jade price, schedule exception and agent access", locale => {
  const c = catalogs[locale];
  expect(c.legal.pricing.sections[0].body[3]).toContain("9.99");
  expect(c.home.faq.items[3].a).toContain("9.99");
  expect(c.welcome.subscriptionPlans).toContain("9.99");
  const name = c.profile.subscriptionTitle.jade;
  expect(c.legal.terms.sections[3].body[3]).toContain(name);
  expect(c.agent.docs.snapshots).toContain(name);
  expect(c.agent.docs.intel).toContain(name);
  for (const page of [c.legal.pricing, c.legal.terms, c.legal.privacy]) expect(page.lastUpdated).toContain("2026-10-08");
  const newCopy = [c.jade.intro, c.jade.price, c.profile.subscriptionDescription.jade, c.profile.subscriptionJadeCadence, c.profile.jadeManage, c.profile.jadeScheduleNote, c.legal.pricing.sections[0].body[3], c.legal.terms.sections[3].body[3]].join(" ");
  expect(newCopy).not.toMatch(/\b(should|recommend\w*|buy|sell)\b|\u5efa\u8bae|\u5efa\u8b70|\u4e70\u5165|\u8cb7\u5165|\u5356\u51fa|\u8ce3\u51fa/i);
});
