import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { usePathname, getUser, onAuthStateChange, logout } = vi.hoisted(() => ({
  usePathname: vi.fn(),
  getUser: vi.fn(),
  onAuthStateChange: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("next/navigation", () => ({ usePathname }));
vi.mock("@/lib/supabase/browser", () => ({
  createClient: () => ({
    auth: {
      getUser,
      onAuthStateChange: (cb: (...args: unknown[]) => void) => {
        onAuthStateChange(cb);
        return { data: { subscription: { unsubscribe: vi.fn() } } };
      },
    },
  }),
}));
// Server Action import would drag in lib/supabase/server.ts's `server-only`
// guard under vitest (no Next compiler pass to stub it) — mock like the
// other suites do.
vi.mock("@/lib/auth-actions", () => ({ logout }));

import { LocaleProvider } from "@/app/_components/locale-provider";

import { GetStartedMenu } from "./get-started-menu";

function renderMenu(route = "/holdings") {
  usePathname.mockReturnValue(route);
  // GetStartedMenu reads home-messages via useHomeMessages, which requires
  // the provider — same wrapper the site-header suite uses.
  return render(
    <LocaleProvider>
      <GetStartedMenu />
    </LocaleProvider>,
  );
}

// The trigger only exists once the async session check resolves out of
// "checking" (the component renders nothing until then), so every entry
// point must await it.
async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  const trigger = await screen.findByRole("button", { name: /get started/i });
  await user.click(trigger);
  return waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
}

describe("GetStartedMenu", () => {
  beforeEach(() => {
    getUser.mockReturnValue(new Promise(() => {})); // checking by default
    vi.clearAllMocks();
  });

  it("renders nothing while the verified session check is in flight", () => {
    const { container } = renderMenu();

    expect(container).toBeEmptyDOMElement();
  });

  it("starts closed and opens on click", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    const user = userEvent.setup();
    renderMenu();

    const trigger = await screen.findByRole("button", { name: /get started/i });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    await user.click(trigger);

    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
  });

  it("closes on Escape", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    const user = userEvent.setup();
    renderMenu();
    await openMenu(user);

    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
  });

  describe("logged out (R4)", () => {
    it("offers exactly one entry — Log in — with no holdings, signup or account rows", async () => {
      getUser.mockResolvedValue({ data: { user: null } });
      const user = userEvent.setup();
      renderMenu();
      await openMenu(user);

      const menu = screen.getByRole("menu");
      expect(menu.textContent).toContain("Log in");
      expect(menu.textContent).not.toContain("Holdings");
      expect(menu.textContent).not.toContain("Sign up");
      expect(menu.textContent).not.toContain("Log out");
      expect(menu.querySelector('input[type="email"]')).toBeNull();
    });

    it("links the Log in entry to /login", async () => {
      getUser.mockResolvedValue({ data: { user: null } });
      const user = userEvent.setup();
      renderMenu();
      await openMenu(user);

      expect(screen.getByRole("menuitem", { name: "Log in" })).toHaveAttribute(
        "href",
        "/login",
      );
    });
  });

  describe("logged in", () => {
    it("shows Holdings, the account email, and Log out — but no Log in or Signup", async () => {
      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      const user = userEvent.setup();
      renderMenu();
      await openMenu(user);

      const menu = screen.getByRole("menu");
      expect(screen.getByRole("menuitem", { name: "Holdings" })).toHaveAttribute(
        "href",
        "/holdings",
      );
      expect(menu.textContent).toContain("a@b.com");
      expect(screen.getByRole("menuitem", { name: "Log out" })).toBeInTheDocument();
      expect(menu.textContent).not.toContain("Log in");
      expect(menu.textContent).not.toContain("Sign up");
    });

    it("calls the logout Server Action from the Log out item", async () => {
      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      const user = userEvent.setup();
      renderMenu();
      await openMenu(user);

      await user.click(screen.getByRole("menuitem", { name: "Log out" }));

      await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
    });

    it("omits reserved future entries whose routes have not shipped", async () => {
      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      const user = userEvent.setup();
      renderMenu();
      await openMenu(user);

      const menu = screen.getByRole("menu");
      expect(menu.textContent).not.toContain("Settings");
      expect(menu.textContent).not.toContain("Vigil");
      expect(menu.textContent).not.toContain("Questionnaire");
    });
  });

  describe("labels", () => {
    it("uses the zh nav labels on the home route when zh is selected", async () => {
      // This test environment's jsdom localStorage is broken (`setItem is not
      // a function`); stub a working in-memory Storage like the old suite.
      const store = new Map<string, string>();
      Object.defineProperty(window, "localStorage", {
        value: {
          getItem: (key: string) => store.get(key) ?? null,
          setItem: (key: string, value: string) => void store.set(key, value),
          removeItem: (key: string) => void store.delete(key),
          clear: () => store.clear(),
        },
        configurable: true,
      });
      window.localStorage.setItem("portfonia:locale", "zh");

      getUser.mockResolvedValue({ data: { user: null } });
      const user = userEvent.setup();
      renderMenu("/");

      await user.click(
        await screen.findByRole("button", { name: "开始使用" }),
      );

      await waitFor(() =>
        expect(screen.getByRole("menuitem", { name: "登录" })).toBeInTheDocument(),
      );
    });

    it("stays English on non-home routes even when zh is selected (messages.ts has no zh map yet)", async () => {
      const store = new Map<string, string>();
      Object.defineProperty(window, "localStorage", {
        value: {
          getItem: (key: string) => store.get(key) ?? null,
          setItem: (key: string, value: string) => void store.set(key, value),
          removeItem: (key: string) => void store.delete(key),
          clear: () => store.clear(),
        },
        configurable: true,
      });
      window.localStorage.setItem("portfonia:locale", "zh");

      getUser.mockResolvedValue({ data: { user: null } });
      const user = userEvent.setup();
      renderMenu("/holdings");

      await user.click(
        await screen.findByRole("button", { name: "Get Started" }),
      );

      await waitFor(() =>
        expect(screen.getByRole("menuitem", { name: "Log in" })).toBeInTheDocument(),
      );
    });
  });
});
