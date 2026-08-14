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

describe("SiteHeader", () => {
  it("always shows the brand link (to home) and a Holdings entry, on any route", () => {
    usePathname.mockReturnValue("/holdings");
    renderHeader();

    expect(screen.getByRole("link", { name: /portfonia/i })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: /holdings/i })).toHaveAttribute(
      "href",
      "/holdings",
    );
  });

  it("hides the marketing anchors and the locale switcher outside the home route", () => {
    usePathname.mockReturnValue("/holdings");
    renderHeader();

    expect(
      screen.queryByRole("link", { name: /what we don't do/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /language/i })).not.toBeInTheDocument();
  });

  it("shows the marketing anchors and the locale switcher on the home route", () => {
    usePathname.mockReturnValue("/");
    renderHeader();

    expect(screen.getByRole("link", { name: /what we don't do/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /how it works/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /sample briefing/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /faq/i })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /language/i })).toBeInTheDocument();
  });

  it("uses the same floating-pill dark chrome on every route (not just home)", () => {
    usePathname.mockReturnValue("/holdings");
    const { container } = renderHeader();

    expect(container.querySelector("nav")).toHaveClass("rounded-full", "backdrop-blur-md");
  });
});
