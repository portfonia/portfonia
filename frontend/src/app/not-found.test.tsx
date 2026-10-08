import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { catalogs, type Locale } from "@/locales";
import { LocaleProvider } from "./_components/locale-provider";
import NotFound from "./not-found";
it.each(["en", "zh-Hans", "zh-Hant"] as Locale[])("renders a localized %s 404 and home link", (locale) => {
  render(<LocaleProvider routeLocale={locale}><NotFound /></LocaleProvider>);
  expect(screen.getByRole("heading", { name: catalogs[locale].notFound.title })).toBeInTheDocument();
  expect(screen.getByText(catalogs[locale].notFound.body)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: catalogs[locale].notFound.homeLink })).toHaveAttribute("href", locale === "en" ? "/" : `/${locale}`);
});
