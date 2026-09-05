import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getUser, onAuthStateChange, logout } = vi.hoisted(() => ({
  getUser: vi.fn(),
  onAuthStateChange: vi.fn(),
  logout: vi.fn(),
}));

// GetStartedMenu itself no longer reads the route (issue #209 unified the
// menu across routes), but its useSession() dependency still calls
// usePathname() purely as a "something navigated, re-verify" effect
// trigger — real Next.js usePathname() throws outside an App Router context,
// so it still needs mocking here even though no test varies it by route.
vi.mock("next/navigation", () => ({ usePathname: () => "/holdings" }));
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...rest
  }: {
    href: string;
    children?: unknown;
  }) => (
    <a href={href} data-next-link="true" {...rest}>
      {children as never}
    </a>
  ),
}));
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

import { LocaleProvider, useLocale } from "@/app/_components/locale-provider";
import {
  markPendingLogin,
  clearPendingLogin,
  __resetSessionSignalsForTests,
} from "@/hooks/use-session";

import { GetStartedMenu } from "./get-started-menu";

function LocaleToggle() {
  const { locale, setLocale } = useLocale();
  return (
    <button
      type="button"
      onClick={() => setLocale(locale === "zh-Hans" ? "en" : "zh-Hans")}
    >
      toggle-locale
    </button>
  );
}

// issue #209: GetStartedMenu no longer branches on route (that split between
// home-messages.ts's `nav.*` and this file's own English-only `menu.*` was
// the root cause of the mixed-language menu bug, issue #207/PR #208) — every
// test below renders on /holdings specifically to prove the fix: a non-home
// route now honors the selected locale exactly like home used to.
function renderMenu() {
  return render(
    <LocaleProvider>
      <GetStartedMenu />
    </LocaleProvider>,
  );
}

function withLocaleStorage(initial?: string) {
  const store = new Map<string, string>();
  if (initial) store.set("portfonia:locale", initial);
  Object.defineProperty(window, "localStorage", {
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
    },
    configurable: true,
  });
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
    __resetSessionSignalsForTests();
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
      expect(screen.getByRole("menuitem", { name: "Log in" })).toHaveAttribute(
        "data-next-link",
        "true",
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

    it("no longer offers a separate Edit holdings entry (issue #319 item 1)", async () => {
      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      const user = userEvent.setup();
      renderMenu();
      await openMenu(user);

      expect(screen.queryByRole("menuitem", { name: "Edit holdings" })).not.toBeInTheDocument();
      expect(screen.getByRole("menuitem", { name: "Holdings" })).toHaveAttribute(
        "href",
        "/holdings",
      );
    });

    it("offers a Profile entry linking to /profile as the first item (issue #220)", async () => {
      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      const user = userEvent.setup();
      renderMenu();
      await openMenu(user);

      expect(screen.getByRole("menuitem", { name: "Profile" })).toHaveAttribute(
        "href",
        "/profile",
      );

      const itemLabels = screen
        .getAllByRole("menuitem")
        .map((el) => el.textContent);
      expect(itemLabels[0]).toBe("Profile");
    });

    it("calls the logout Server Action without a reason from the manual Log out item", async () => {
      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      const user = userEvent.setup();
      renderMenu();
      await openMenu(user);

      await user.click(screen.getByRole("menuitem", { name: "Log out" }));

      await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
      // Manual logout passes NO reason (vs the idle hook's "expired").
      expect(logout).toHaveBeenCalledWith();
    });

    it("restores the authed menu and shows an error when logout() rejects", async () => {
      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      logout.mockRejectedValue(new Error("auth.portfonia.com unreachable"));
      const user = userEvent.setup();
      renderMenu();
      await openMenu(user);

      await user.click(screen.getByRole("menuitem", { name: "Log out" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Sign out failed. Try again.",
      );
      await openMenu(user);
      expect(screen.getByRole("menuitem", { name: "Log out" })).toBeInTheDocument();
      expect(screen.getByText("a@b.com")).toBeInTheDocument();
    });

    it("re-renders the logout-error alert in the current locale after a language switch", async () => {
      withLocaleStorage("zh-Hans");

      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      logout.mockRejectedValue(new Error("auth.portfonia.com unreachable"));
      const user = userEvent.setup();
      render(
        <LocaleProvider>
          <LocaleToggle />
          <GetStartedMenu />
        </LocaleProvider>,
      );

      await user.click(await screen.findByRole("button", { name: "开始使用" }));
      await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
      await user.click(screen.getByRole("menuitem", { name: "退出登录" }));

      expect(await screen.findByRole("alert")).toHaveTextContent("退出失败，请重试。");

      await user.click(screen.getByRole("button", { name: "toggle-locale" }));

      expect(screen.getByRole("alert")).toHaveTextContent("Sign out failed. Try again.");
      expect(screen.queryByText("退出失败，请重试。")).not.toBeInTheDocument();
    });

    it("does not treat a NEXT_REDIRECT rejection from logout() as a failure", async () => {
      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      logout.mockRejectedValue({
        digest: "NEXT_REDIRECT;replace;/login;307;",
      });
      const user = userEvent.setup();
      renderMenu();
      await openMenu(user);

      await user.click(screen.getByRole("menuitem", { name: "Log out" }));

      await openMenu(user);
      expect(screen.getByRole("menuitem", { name: "Log in" })).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.queryByRole("menuitem", { name: "Log out" })).not.toBeInTheDocument();
    });

    it("switches to the logged-out menu the instant Log out is clicked, without waiting on getUser()", async () => {
      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      const user = userEvent.setup();
      renderMenu();
      await openMenu(user);

      // getUser() never resolves again after the click — the guest state
      // must come from the click itself (optimistic), not a round-trip.
      getUser.mockReturnValue(new Promise(() => {}));
      await user.click(screen.getByRole("menuitem", { name: "Log out" }));
      // Clicking a menuitem closes the dropdown (Base UI default) — reopen
      // it to inspect what the now-guest session renders.
      await openMenu(user);

      await waitFor(() =>
        expect(screen.getByRole("menuitem", { name: "Log in" })).toBeInTheDocument(),
      );
      expect(screen.queryByRole("menuitem", { name: "Holdings" })).not.toBeInTheDocument();
    });

    it("omits reserved future entries whose routes have not shipped", async () => {
      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      const user = userEvent.setup();
      renderMenu();
      await openMenu(user);

      const menu = screen.getByRole("menu");
      expect(menu.textContent).not.toContain("Settings");
      expect(menu.textContent).not.toContain("Vigil");
    });
  });

  describe("login-pending transition", () => {
    it("shows a Logging in... placeholder instead of rendering nothing when markPendingLogin() preceded this mount", async () => {
      markPendingLogin();
      getUser.mockReturnValue(new Promise(() => {}));
      renderMenu();

      expect(await screen.findByText("Logging in...")).toBeInTheDocument();
    });

    it("renders nothing on an ordinary mount with no prior markPendingLogin()", () => {
      getUser.mockReturnValue(new Promise(() => {}));
      const { container } = renderMenu();

      expect(container).toBeEmptyDOMElement();
    });

    it("does not render the pending placeholder after the signal was cleared (failed login must not leak into a later navigation)", () => {
      markPendingLogin();
      clearPendingLogin();
      getUser.mockReturnValue(new Promise(() => {}));
      const { container } = renderMenu();

      expect(container).toBeEmptyDOMElement();
      expect(screen.queryByText("Logging in...")).not.toBeInTheDocument();
    });

    it("replaces the placeholder with the real menu once verification resolves", async () => {
      markPendingLogin();
      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      renderMenu();

      await waitFor(() =>
        expect(screen.getByRole("button", { name: /get started/i })).toBeInTheDocument(),
      );
      expect(screen.queryByText("Logging in...")).not.toBeInTheDocument();
    });
  });

  describe("labels (issue #209: one catalog, no more per-route split)", () => {
    it("uses the zh-Hans nav labels on a non-home route when zh-Hans is selected", async () => {
      // This test environment's jsdom localStorage is broken (`setItem is
      // not a function`); stub a working in-memory Storage like the old
      // suite.
      withLocaleStorage("zh-Hans");

      getUser.mockResolvedValue({ data: { user: null } });
      const user = userEvent.setup();
      renderMenu();

      await user.click(
        await screen.findByRole("button", { name: "开始使用" }),
      );

      await waitFor(() =>
        expect(screen.getByRole("menuitem", { name: "登录" })).toBeInTheDocument(),
      );
    });

    it("uses the zh-Hans Holdings label on a non-home route when logged in — the exact bug this issue fixes (issue #207/PR #208)", async () => {
      withLocaleStorage("zh-Hans");

      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      const user = userEvent.setup();
      renderMenu();

      await user.click(
        await screen.findByRole("button", { name: "开始使用" }),
      );

      await waitFor(() =>
        expect(screen.getByRole("menuitem", { name: "持仓" })).toBeInTheDocument(),
      );
      expect(screen.queryByRole("menuitem", { name: "Holdings" })).not.toBeInTheDocument();
    });

    it("uses the zh-Hans Profile label on a non-home route when logged in (not a mixed-language menu)", async () => {
      withLocaleStorage("zh-Hans");

      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      const user = userEvent.setup();
      renderMenu();

      await user.click(
        await screen.findByRole("button", { name: "开始使用" }),
      );

      await waitFor(() =>
        expect(screen.getByRole("menuitem", { name: "个人资料" })).toBeInTheDocument(),
      );
      expect(screen.queryByRole("menuitem", { name: "Profile" })).not.toBeInTheDocument();
    });

    // Issue #350 item 4 lifted zh-Hant's UNREVIEWED_LOCALES gate (a
    // deliberate product-owner decision, see src/locales/README.md's
    // "zh-Hant review status") — a stored zh-Hant value now restores and
    // renders like any other supported locale, superseding the PR #226
    // fallback behavior this test used to lock in.
    it("restores zh-Hant from a stored locale and renders its own copy (issue #350 item 4: gate lifted)", async () => {
      withLocaleStorage("zh-Hant");

      getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
      const user = userEvent.setup();
      renderMenu();

      await user.click(await screen.findByRole("button", { name: "開始使用" }));

      await waitFor(() =>
        expect(screen.getByRole("menuitem", { name: "持倉" })).toBeInTheDocument(),
      );
      expect(screen.queryByRole("menuitem", { name: "Holdings" })).not.toBeInTheDocument();
    });

    it("uses the zh-Hans Logging in... label when zh-Hans is selected", async () => {
      withLocaleStorage("zh-Hans");

      markPendingLogin();
      getUser.mockReturnValue(new Promise(() => {}));
      renderMenu();

      expect(await screen.findByText("正在登录...")).toBeInTheDocument();
      expect(screen.queryByText("Logging in...")).not.toBeInTheDocument();
    });
  });
});
