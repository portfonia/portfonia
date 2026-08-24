import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { SiteHeader } from "./site-header";

const { usePathname, getUser } = vi.hoisted(() => ({
  usePathname: vi.fn(),
  getUser: vi.fn(),
}));

vi.mock("next/navigation", () => ({ usePathname }));

// AuthStatus (embedded in every SiteHeader render) calls the browser
// Supabase client on mount — stub it so these route-chrome tests don't
// depend on real env vars or network. Defaults to a pending (never-
// resolving) promise so AuthStatus stays in its "loading" (renders
// nothing) state for the chrome tests below, which aren't about login
// state itself (see auth-status.test.tsx for that) — individual tests in
// the "login/logout entry" block override this to check label i18n.
vi.mock("@/lib/supabase/browser", () => ({
  createClient: () => ({
    auth: {
      getUser,
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe: vi.fn() } } }),
    },
  }),
}));

// AuthStatus's logout button imports this Server Action directly (Next
// compiles that into a client-safe fetch stub in a real build — vitest has
// no such compiler pass, so importing the real module would drag in
// lib/supabase/server.ts's `server-only` guard, which throws outside
// Next's own bundler). Mock it the same way auth-status.test.tsx does.
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

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
  beforeEach(() => {
    getUser.mockReturnValue(new Promise(() => {}));
  });

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

  describe("login/logout entry", () => {
    // This test environment's jsdom localStorage is broken (`setItem is
    // not a function` — a jsdom/Node webstorage version mismatch, unrelated
    // to LocaleProvider's own code, which already handles a genuinely
    // inaccessible localStorage via try/catch). Stub a working in-memory
    // Storage so these two locale-switch tests aren't testing that quirk.
    function stubLocalStorage() {
      const store = new Map<string, string>();
      const fake: Pick<Storage, "getItem" | "setItem" | "removeItem" | "clear"> = {
        getItem: (key) => store.get(key) ?? null,
        setItem: (key, value) => void store.set(key, value),
        removeItem: (key) => void store.delete(key),
        clear: () => store.clear(),
      };
      Object.defineProperty(window, "localStorage", { value: fake, configurable: true });
    }

    it("shows a Log in link when logged out, on every route", async () => {
      getUser.mockResolvedValue({ data: { user: null } });
      usePathname.mockReturnValue("/holdings");
      renderHeader();

      expect(await screen.findByRole("link", { name: "Log in" })).toHaveAttribute(
        "href",
        "/login",
      );
    });

    it("uses the zh nav label on the home route when zh is selected", async () => {
      stubLocalStorage();
      getUser.mockResolvedValue({ data: { user: null } });
      window.localStorage.setItem("portfonia:locale", "zh");
      usePathname.mockReturnValue("/");
      renderHeader();

      expect(await screen.findByRole("link", { name: "登录" })).toBeInTheDocument();
    });

    it("stays English on non-home routes even when zh is selected (messages.ts has no zh map yet)", async () => {
      stubLocalStorage();
      getUser.mockResolvedValue({ data: { user: null } });
      window.localStorage.setItem("portfonia:locale", "zh");
      usePathname.mockReturnValue("/holdings");
      renderHeader();

      expect(await screen.findByRole("link", { name: "Log in" })).toBeInTheDocument();
    });

    it("shows the account email and a Log out button when logged in", async () => {
      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      usePathname.mockReturnValue("/holdings");
      renderHeader();

      expect(await screen.findByText("a@b.com")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Log out" })).toBeInTheDocument();
    });
  });
});
