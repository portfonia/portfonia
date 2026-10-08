import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderToString } from "react-dom/server";
import { useTranslations } from "next-intl";
const navigation = vi.hoisted(() => ({ pathname: "/", push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: navigation.push }), usePathname: () => navigation.pathname }));
import { LocaleProvider, useLocale } from "./locale-provider";

function LocaleSwitcherProbe() {
  const { locale, setLocale } = useLocale();
  return (
    <button type="button" onClick={() => setLocale("zh-Hans")}>
      current: {locale}
    </button>
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

// blacktomb42 review (PR #226): html/lang never actually followed the
// selected locale — AppShell only ever set `lang` on an inner wrapper div,
// never on the real `<html>` element, and layout.tsx hardcodes
// `<html lang="en">` server-side (which SSR must, since locale is
// client-only — see src/locales/README.md). LocaleProvider is the one place
// `locale` state changes (both the storage restore and setLocale), so it
// owns syncing the real document element.
describe("LocaleProvider keeps document.documentElement.lang in sync", () => {
  const originalLang = document.documentElement.lang;

  beforeEach(() => {
    document.documentElement.lang = "";
  });

  afterEach(() => {
    document.documentElement.lang = originalLang;
  });

  it("sets documentElement.lang to the default locale on mount", async () => {
    render(
      <LocaleProvider routeLocale={null}>
        <p>content</p>
      </LocaleProvider>,
    );

    await waitFor(() => expect(document.documentElement.lang).toBe("en"));
  });

  it("updates documentElement.lang when setLocale is called", async () => {
    const user = userEvent.setup();
    render(
      <LocaleProvider routeLocale={null}>
        <LocaleSwitcherProbe />
      </LocaleProvider>,
    );
    await waitFor(() => expect(document.documentElement.lang).toBe("en"));

    await user.click(screen.getByRole("button"));

    await waitFor(() => expect(document.documentElement.lang).toBe("zh-Hans"));
  });

  it("updates documentElement.lang to a locale restored from localStorage", async () => {
    withLocaleStorage("zh-Hans");

    render(
      <LocaleProvider routeLocale={null}>
        <p>content</p>
      </LocaleProvider>,
    );

    await waitFor(() => expect(document.documentElement.lang).toBe("zh-Hans"));
  });

  // Issue #350 item 4 lifted zh-Hant's UNREVIEWED_LOCALES gate (a deliberate
  // product-owner decision, see src/locales/README.md's "zh-Hant review
  // status") — a stored zh-Hant value now restores like any other supported
  // locale, superseding the PR #226 behavior this test used to lock in.
  it("restores a stored zh-Hant locale (issue #350 item 4: gate lifted)", async () => {
    withLocaleStorage("zh-Hant");

    render(
      <LocaleProvider routeLocale={null}>
        <p>content</p>
      </LocaleProvider>,
    );

    await waitFor(() => expect(document.documentElement.lang).toBe("zh-Hant"));
  });
});

// blacktomb42 round-2 review (PR #226, non-blocking): the legacy "zh" ->
// "zh-Hans" migration only ever updated in-memory state, never rewrote
// localStorage — every future page load re-interpreted the same stale "zh"
// value instead of the migration actually completing once.
describe("LocaleProvider migrates a legacy stored 'zh' value", () => {
  it("rewrites localStorage to zh-Hans, not just the in-memory locale", async () => {
    withLocaleStorage("zh");

    render(
      <LocaleProvider routeLocale={null}>
        <p>content</p>
      </LocaleProvider>,
    );

    await waitFor(() => expect(document.documentElement.lang).toBe("zh-Hans"));
    expect(window.localStorage.getItem("portfonia:locale")).toBe("zh-Hans");
  });
});


function SeoLocaleProbe() {
  const { locale, setLocale } = useLocale();
  const t = useTranslations("seo");
  return <><span data-testid="locale">{locale}</span><span>{t("ogImageAlt")}</span><button onClick={() => setLocale("en")}>English</button></>;
}

describe("issue #702 route locale", () => {
  beforeEach(() => { navigation.push.mockClear(); navigation.pathname = "/"; window.history.replaceState(null, "", "/"); withLocaleStorage(); });
  it("renders Chinese on the server and ignores a conflicting stored locale", () => {
    withLocaleStorage("zh-Hant");
    expect(renderToString(<LocaleProvider routeLocale="zh-Hans"><SeoLocaleProbe /></LocaleProvider>)).toContain("zh-Hans");
    render(<LocaleProvider routeLocale="zh-Hans"><SeoLocaleProbe /></LocaleProvider>);
    expect(screen.getByTestId("locale")).toHaveTextContent("zh-Hans");
    expect(window.localStorage.getItem("portfonia:locale")).toBe("zh-Hans");
  });
  it("preserves initial localStorage restore on a non-SEO page", () => {
    window.history.replaceState(null, "", "/login");
    withLocaleStorage("zh-Hant");
    render(<LocaleProvider routeLocale={null}><SeoLocaleProbe /></LocaleProvider>);
    expect(screen.getByTestId("locale")).toHaveTextContent("zh-Hant");
  });
  it("setLocale persists without navigation on an SEO URL with query and hash", async () => {
    window.history.replaceState(null, "", "/zh-Hans/pricing?x=1#plans");
    render(<LocaleProvider routeLocale="zh-Hans"><SeoLocaleProbe /></LocaleProvider>);
    await userEvent.click(screen.getByRole("button", { name: "English" }));
    expect(navigation.push).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("portfonia:locale")).toBe("en");
  });
  it("setLocale persists without navigation on a plain SEO URL", async () => {
    window.history.replaceState(null, "", "/zh-Hans/pricing");
    render(<LocaleProvider routeLocale="zh-Hans"><SeoLocaleProbe /></LocaleProvider>);
    await userEvent.click(screen.getByRole("button", { name: "English" }));
    expect(navigation.push).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("portfonia:locale")).toBe("en");
  });
  it("does not navigate on an auth page", async () => {
    window.history.replaceState(null, "", "/login");
    render(<LocaleProvider routeLocale="zh-Hans"><SeoLocaleProbe /></LocaleProvider>);
    await userEvent.click(screen.getByRole("button", { name: "English" }));
    expect(navigation.push).not.toHaveBeenCalled();
  });
  it("7c follows the real URL on pathname changes and browser Back, leaving auth state alone", () => {
    window.history.replaceState(null, "", "/zh-Hans/pricing"); navigation.pathname = "/pricing";
    const view = render(<LocaleProvider routeLocale="zh-Hans"><SeoLocaleProbe /></LocaleProvider>);
    window.history.pushState(null, "", "/pricing"); navigation.pathname = "/";
    view.rerender(<LocaleProvider routeLocale="zh-Hans"><SeoLocaleProbe /></LocaleProvider>);
    expect(screen.getByTestId("locale")).toHaveTextContent("en");
    act(() => { window.history.replaceState(null, "", "/zh-Hans/pricing"); window.dispatchEvent(new PopStateEvent("popstate")); });
    expect(screen.getByTestId("locale")).toHaveTextContent("zh-Hans");
    window.history.pushState(null, "", "/login"); navigation.pathname = "/login";
    view.rerender(<LocaleProvider routeLocale="zh-Hans"><SeoLocaleProbe /></LocaleProvider>);
    expect(screen.getByTestId("locale")).toHaveTextContent("zh-Hans");
  });
});


describe("7h/7j URL authority and stored app preference", () => {
  beforeEach(() => { navigation.push.mockClear(); navigation.pathname = "/pricing"; window.history.replaceState(null, "", "/pricing"); withLocaleStorage("zh-Hans"); });
  it("7h renders English without overwriting storage, then restores Chinese on a fresh login mount", () => {
    const view = render(<LocaleProvider routeLocale="en"><SeoLocaleProbe /></LocaleProvider>);
    expect(screen.getByTestId("locale")).toHaveTextContent("en");
    expect(window.localStorage.getItem("portfonia:locale")).toBe("zh-Hans");
    view.unmount();
    window.history.replaceState(null, "", "/login"); navigation.pathname = "/login";
    render(<LocaleProvider routeLocale={null}><SeoLocaleProbe /></LocaleProvider>);
    expect(screen.getByTestId("locale")).toHaveTextContent("zh-Hans");
  });
  it("7j restores storage on soft navigation to login", () => {
    const view = render(<LocaleProvider routeLocale="en"><SeoLocaleProbe /></LocaleProvider>);
    window.history.pushState(null, "", "/login"); navigation.pathname = "/login";
    view.rerender(<LocaleProvider routeLocale="en"><SeoLocaleProbe /></LocaleProvider>);
    expect(screen.getByTestId("locale")).toHaveTextContent("zh-Hans");
    expect(document.documentElement.lang).toBe("zh-Hans");
  });
  it("7j reapplies html lang when routeLocale changes but the stored state is unchanged", () => {
    const view = render(<LocaleProvider routeLocale="en"><SeoLocaleProbe /></LocaleProvider>);
    window.history.pushState(null, "", "/profile"); navigation.pathname = "/profile";
    view.rerender(<LocaleProvider routeLocale="en"><SeoLocaleProbe /></LocaleProvider>);
    // Simulate the root layout reapplying its unprefixed html lang during refresh.
    document.documentElement.lang = "en";
    view.rerender(<LocaleProvider routeLocale={null}><SeoLocaleProbe /></LocaleProvider>);
    expect(screen.getByTestId("locale")).toHaveTextContent("zh-Hans");
    expect(document.documentElement.lang).toBe("zh-Hans");
  });
  it("syncs from the real SEO URL when only routeLocale changes", () => {
    const view = render(<LocaleProvider routeLocale="en"><SeoLocaleProbe /></LocaleProvider>);
    window.history.replaceState(null, "", "/zh-Hant/pricing");
    view.rerender(<LocaleProvider routeLocale="zh-Hant"><SeoLocaleProbe /></LocaleProvider>);
    expect(screen.getByTestId("locale")).toHaveTextContent("zh-Hant");
    expect(document.documentElement.lang).toBe("zh-Hant");
  });
});


it("7j restores html lang on a prop-only refresh with unchanged locale state", () => {
  withLocaleStorage("zh-Hans");
  window.history.replaceState(null, "", "/zh-Hans/pricing"); navigation.pathname = "/pricing";
  const view = render(<LocaleProvider routeLocale="zh-Hans"><SeoLocaleProbe /></LocaleProvider>);
  window.history.replaceState(null, "", "/profile"); navigation.pathname = "/profile";
  view.rerender(<LocaleProvider routeLocale="zh-Hans"><SeoLocaleProbe /></LocaleProvider>);
  expect(screen.getByTestId("locale")).toHaveTextContent("zh-Hans");
  document.documentElement.lang = "en";
  view.rerender(<LocaleProvider routeLocale={null}><SeoLocaleProbe /></LocaleProvider>);
  expect(screen.getByTestId("locale")).toHaveTextContent("zh-Hans");
  expect(document.documentElement.lang).toBe("zh-Hans");
});
