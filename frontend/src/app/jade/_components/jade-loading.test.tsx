import { readFileSync } from "node:fs";
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { catalogs } from "@/locales";

it.each(["en", "zh-Hans", "zh-Hant"] as const)("733 A7 %s route loading container title and polite status", async locale => {
  const { default: Loading } = await import("../loading");
  const { container } = render(<LocaleProvider routeLocale={locale}><Loading /></LocaleProvider>);
  const main = container.querySelector("main");
  expect(main).toHaveClass("mx-auto", "w-full", "max-w-2xl", "px-4", "py-10", "sm:px-6");
  expect(main).toContainElement(screen.getByRole("heading", { level: 1, name: catalogs[locale].jade.title }));
  expect(screen.getByRole("status").textContent).toBe(catalogs[locale].common.calculating);
  expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
  expect(screen.getByRole("status").querySelector("svg")).toHaveAttribute("aria-hidden", "true");
});
it("733 A7 loading imports exclude charts API and sections", () => {
  for (const file of ["./jade-loading.tsx", "../loading.tsx"]) {
    const source = readFileSync(new URL(file, import.meta.url), "utf8");
    expect(source).not.toMatch(/(?:from\s*|import\s*\()["'][^"']*(?:recharts|@\/lib\/api|(?:replay|tail-risk|stress|style|tracking)-section)/);
  }
});
