import { cleanup, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { catalogs, type Locale } from "@/locales";
const route = vi.hoisted(() => ({ locale: "en" as Locale }));
vi.mock("@/lib/seo-server", () => ({ getRouteLocale: async () => route.locale }));
import GuidePage from "./page";

it("775 guests see the complete five-section pipeline guide in all four locales", async () => {
  for (const locale of ["en", "es", "zh-Hans", "zh-Hant"] as const) {
    route.locale = locale;
    const view = render(await GuidePage());
    const copy = catalogs[locale].agentGuide;
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(copy.title);
    expect(screen.getAllByRole("heading", { level: 2 }).map(h => h.textContent)).toEqual([copy.whatHeading, copy.howHeading, locale === "en" ? "Each run, step by step" : locale === "es" ? "Cada ejecuci\u00f3n, paso a paso" : locale === "zh-Hans" ? "\u6bcf\u6b21\u8fd0\u884c\u7684\u6b65\u9aa4" : "\u6bcf\u6b21\u57f7\u884c\u7684\u6b65\u9a5f", locale === "en" ? "Limits and safeguards" : locale === "es" ? "L\u00edmites y medidas de protecci\u00f3n" : locale === "zh-Hans" ? "\u9650\u5236\u4e0e\u4fdd\u62a4\u63aa\u65bd" : "\u9650\u5236\u8207\u4fdd\u8b77\u63aa\u65bd", copy.effectHeading]);
    for (const text of [copy.what, copy.how, copy.effect]) expect(screen.getByText(text)).toBeInTheDocument();
    expect(view.container.querySelectorAll("a")).toHaveLength(0);
    expect(view.container.querySelectorAll("form, input, code")).toHaveLength(0);
    cleanup();
  }
});
