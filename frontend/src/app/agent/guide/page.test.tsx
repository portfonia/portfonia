import { cleanup, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { catalogs, type Locale } from "@/locales";
import { localizedPath } from "@/lib/seo";
const route = vi.hoisted(() => ({ locale: "en" as Locale }));
vi.mock("@/lib/seo-server", () => ({ getRouteLocale: async () => route.locale }));
import GuidePage from "./page";

it("763 guests see the guide copy and localized token link in all four locales", async () => {
  for (const locale of ["en", "es", "zh-Hans", "zh-Hant"] as const) {
    route.locale = locale;
    const view = render(await GuidePage());
    const copy = catalogs[locale].agentGuide;
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(copy.title);
    expect(screen.getAllByRole("heading", { level: 2 }).map(h => h.textContent)).toEqual([copy.whatHeading, copy.howHeading, copy.effectHeading]);
    for (const text of [copy.what, copy.how, copy.effect]) expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: copy.tokenLink })).toHaveAttribute("href", localizedPath("/agent", locale));
    expect(view.container.querySelectorAll("form, input, code")).toHaveLength(0);
    cleanup();
  }
});
