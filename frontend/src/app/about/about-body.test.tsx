import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { catalogs, type Locale } from "@/locales";
import { LocaleProvider } from "../_components/locale-provider";
import { AboutBody } from "./about-body";
it.each(["en", "zh-Hans", "zh-Hant"] as Locale[])("renders five %s sections and localized public links", (locale) => {
  render(<LocaleProvider routeLocale={locale}><AboutBody /></LocaleProvider>);
  expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(5);
  for (const section of catalogs[locale].about.sections) expect(screen.getByRole("heading", { name: section.heading })).toBeInTheDocument();
  const prefix = locale === "en" ? "" : `/${locale}`;
  expect(screen.getByRole("link", { name: catalogs[locale].about.cta.waitlist })).toHaveAttribute("href", `${prefix}/waitlist`);
  expect(screen.getByRole("link", { name: catalogs[locale].about.cta.pricing })).toHaveAttribute("href", `${prefix}/pricing`);
});


it("renders crawlable language choices on About", () => {
  window.history.replaceState(null, "", "/zh-Hans/about");
  const { container } = render(<LocaleProvider routeLocale="zh-Hans"><AboutBody /></LocaleProvider>);
  expect(container.querySelector('a[href="/zh-Hant/about"]')).not.toBeNull();
  window.history.replaceState(null, "", "/");
});
