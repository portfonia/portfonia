import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { __resetReverifyThrottleForTests } from "@/hooks/use-session";
import { SiteHeader } from "./site-header";

const { usePathname, getUser } = vi.hoisted(() => ({
  usePathname: vi.fn(),
  getUser: vi.fn(),
}));

vi.mock("next/navigation", () => ({ usePathname }));

// GetStartedMenu calls the browser Supabase client on mount — stub it so
// these route-chrome tests don't depend on real env vars or network.
// Defaults to a pending (never-resolving) promise so the menu stays in its
// "checking" (renders nothing) state for chrome tests that aren't about
// session state itself (see get-started-menu.test.tsx for that).
vi.mock("@/lib/supabase/browser", () => ({
  createClient: () => ({
    auth: {
      getUser,
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe: vi.fn() } } }),
    },
  }),
}));

// The menu's Log out imports this Server Action directly (Next compiles that
// into a client-safe fetch stub in a real build — vitest has no such
// compiler pass, so importing the real module would drag in
// lib/supabase/server.ts's `server-only` guard, which throws outside Next's
// own bundler).
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

function renderHeader() {
  return render(
    <LocaleProvider>
      <SiteHeader />
    </LocaleProvider>,
  );
}

const ANCHOR_HREFS = ["#boundary", "#how", "#preview", "#faq"];

describe("SiteHeader", () => {
  beforeEach(() => {
    getUser.mockReturnValue(new Promise(() => {}));
    __resetReverifyThrottleForTests();
  });

  it.each(["/", "/holdings"])(
    "renders the same unified bar shape on %s: banner landmark, pill nav, brand, Get Started trigger",
    async (route) => {
      // Resolve the session check so the menu trigger renders (the suite
      // default keeps it pending/"checking").
      getUser.mockResolvedValue({ data: { user: null } });
      usePathname.mockReturnValue(route);
      const { container } = renderHeader();

      expect(screen.getByRole("banner")).toBeInTheDocument();
      expect(container.querySelector("nav")).toHaveClass("rounded-full", "backdrop-blur-md");
      expect(
        container.querySelector('a[href="#top"], a[href="/"]'),
      ).not.toBeNull();
      expect(
        await screen.findByRole("button", { name: /get started/i }),
      ).toBeInTheDocument();
    },
  );

  it.each(["/", "/holdings"])("keeps zero marketing anchor links on %s", (route) => {
    usePathname.mockReturnValue(route);
    const { container } = renderHeader();

    for (const href of ANCHOR_HREFS) {
      expect(container.querySelector(`a[href="${href}"]`)).not.toBeInTheDocument();
    }
  });

  it.each(["/", "/holdings"])(
    "keeps no standalone Holdings button in the bar on %s (it lives inside the menu)",
    (route) => {
      usePathname.mockReturnValue(route);
      renderHeader();

      expect(
        screen.queryByRole("link", { name: /holdings/i }),
      ).not.toBeInTheDocument();
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

  it("hides the locale switcher outside the home route", () => {
    usePathname.mockReturnValue("/holdings");
    renderHeader();

    expect(screen.queryByRole("combobox", { name: /language/i })).not.toBeInTheDocument();
  });

  it("shows the locale switcher on the home route", () => {
    usePathname.mockReturnValue("/");
    renderHeader();

    expect(screen.getByRole("combobox", { name: /language/i })).toBeInTheDocument();
  });
});
