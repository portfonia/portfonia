import { createEvent, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const navigation = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: navigation.push }), usePathname: () => "/" }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { LocaleSwitcher } from "./locale-switcher";

function renderSwitcher() {
  return render(
    <LocaleProvider routeLocale={null}>
      <LocaleSwitcher />
    </LocaleProvider>,
  );
}

describe("LocaleSwitcher", () => {
  it("renders a button trigger (not a native select) carrying the current flag", async () => {
    renderSwitcher();

    const trigger = await screen.findByRole("button", { name: /language/i });
    expect(trigger.querySelector(".fi-us")).toBeInTheDocument();
  });

  it("opens a menu offering all three locales with their own flags", async () => {
    const user = userEvent.setup();
    renderSwitcher();

    await user.click(await screen.findByRole("button", { name: /language/i }));

    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    const en = screen.getByRole("menuitem", { name: "English" });
    const zhHans = screen.getByRole("menuitem", { name: "简体中文" });
    const zhHant = screen.getByRole("menuitem", { name: "繁體中文" });
    expect(en.querySelector(".fi-us")).toBeInTheDocument();
    expect(zhHans.querySelector(".fi-cn")).toBeInTheDocument();
    // Issue #350 item 4: Traditional Chinese -> Taiwan flag, an explicit
    // product-owner choice (not Hong Kong or a generic "CN" flag).
    expect(zhHant.querySelector(".fi-tw")).toBeInTheDocument();
  });

  it("switches locale on click, updating the trigger's flag to the new selection", async () => {
    // The trigger's own accessible name is itself locale-dependent (it
    // renders tMenu("language"), which becomes "语言" once zh-Hans is
    // selected) — query by role alone (this component renders exactly one
    // button) rather than an English-only name regex.
    const user = userEvent.setup();
    renderSwitcher();

    await user.click(await screen.findByRole("button"));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    await user.click(screen.getByRole("menuitem", { name: "简体中文" }));

    await waitFor(() =>
      expect(screen.getByRole("button").querySelector(".fi-cn")).toBeInTheDocument(),
    );
  });
});


describe("issue #702 native language navigation", () => {
  const storageDescriptor = Object.getOwnPropertyDescriptor(window, "localStorage");
  const stored = new Map<string, string>();
  beforeEach(() => {
    navigation.push.mockClear();
    stored.clear();
    Object.defineProperty(window, "localStorage", { configurable: true, value: { getItem: (key: string) => stored.get(key) ?? null, setItem: (key: string, value: string) => stored.set(key, value) } });
    window.history.replaceState(null, "", "/zh-Hans/pricing?x=1#plans");
  });
  afterEach(() => {
    window.history.replaceState(null, "", "/");
    if (storageDescriptor) Object.defineProperty(window, "localStorage", storageDescriptor);
  });
  it("7f opens native anchors on a SEO page and persists locale without preventing default", async () => {
    render(<LocaleProvider routeLocale="zh-Hans"><LocaleSwitcher /></LocaleProvider>);
    await userEvent.click(screen.getByRole("button"));
    const en = await screen.findByRole("menuitem", { name: "English" });
    expect(en.tagName).toBe("A");
    expect(en).toHaveAttribute("href", "/pricing?x=1#plans");
    expect(screen.getByRole("menuitem", { name: "繁體中文" })).toHaveAttribute("href", "/zh-Hant/pricing?x=1#plans");
    const event = createEvent.click(en, { bubbles: true, cancelable: true });
    fireEvent(en, event);
    expect(event.defaultPrevented).toBe(false);
    expect(stored.get("portfonia:locale")).toBe("en");
    expect(navigation.push).not.toHaveBeenCalled();
  });
  it("keeps button options on a non-SEO page", async () => {
    window.history.replaceState(null, "", "/portfolio");
    render(<LocaleProvider routeLocale={null}><LocaleSwitcher /></LocaleProvider>);
    await userEvent.click(screen.getByRole("button"));
    const item = await screen.findByRole("menuitem", { name: "English" });
    expect(item).not.toHaveAttribute("href");
    expect(item.tagName).not.toBe("A");
  });
});
