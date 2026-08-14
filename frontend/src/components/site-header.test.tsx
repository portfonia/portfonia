import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { SiteHeader } from "./site-header";

const { usePathname } = vi.hoisted(() => ({ usePathname: vi.fn() }));

vi.mock("next/navigation", () => ({ usePathname }));

function renderHeader() {
  return render(
    <LocaleProvider>
      <SiteHeader />
    </LocaleProvider>,
  );
}

function holdingsEntryLink() {
  return screen
    .getAllByRole("link")
    .find((el) => el.getAttribute("href") === "/holdings");
}

const ANCHOR_HREFS = ["#boundary", "#how", "#preview", "#faq"];

describe("SiteHeader", () => {
  it.each(["/", "/holdings"])(
    "always has a Holdings entry link (href=/holdings) on %s",
    (route) => {
      usePathname.mockReturnValue(route);
      renderHeader();

      expect(holdingsEntryLink()).toBeDefined();
    },
  );

  it("shows the brand link pointing home, with an in-page #top jump on the home route itself", () => {
    usePathname.mockReturnValue("/");
    renderHeader();

    expect(screen.getByRole("link", { name: /portfonia/i })).toHaveAttribute(
      "href",
      "#top",
    );
  });

  it("shows the brand link pointing to / on non-home routes", () => {
    usePathname.mockReturnValue("/holdings");
    renderHeader();

    expect(screen.getByRole("link", { name: /portfonia/i })).toHaveAttribute(
      "href",
      "/",
    );
  });

  it("hides every marketing anchor and the locale switcher outside the home route", () => {
    usePathname.mockReturnValue("/holdings");
    const { container } = renderHeader();

    for (const href of ANCHOR_HREFS) {
      expect(container.querySelector(`a[href="${href}"]`)).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("combobox", { name: /language/i })).not.toBeInTheDocument();
  });

  it("shows every marketing anchor and the locale switcher on the home route", () => {
    usePathname.mockReturnValue("/");
    const { container } = renderHeader();

    for (const href of ANCHOR_HREFS) {
      expect(container.querySelector(`a[href="${href}"]`)).toBeInTheDocument();
    }
    expect(screen.getByRole("combobox", { name: /language/i })).toBeInTheDocument();
  });

  it("uses the same floating-pill dark chrome on every route (not just home)", () => {
    usePathname.mockReturnValue("/holdings");
    const { container } = renderHeader();

    expect(container.querySelector("nav")).toHaveClass("rounded-full", "backdrop-blur-md");
  });

  it("renders as a <header> landmark on every route", () => {
    usePathname.mockReturnValue("/holdings");
    const { container } = renderHeader();

    expect(container.querySelector("header")).not.toBeNull();
    expect(screen.getByRole("banner")).toBeInTheDocument();
  });
});
