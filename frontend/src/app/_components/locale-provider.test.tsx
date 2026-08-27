import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

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
      <LocaleProvider>
        <p>content</p>
      </LocaleProvider>,
    );

    await waitFor(() => expect(document.documentElement.lang).toBe("en"));
  });

  it("updates documentElement.lang when setLocale is called", async () => {
    const user = userEvent.setup();
    render(
      <LocaleProvider>
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
      <LocaleProvider>
        <p>content</p>
      </LocaleProvider>,
    );

    await waitFor(() => expect(document.documentElement.lang).toBe("zh-Hans"));
  });

  // blacktomb42 review (PR #226): zh-Hant's catalog is LLM-drafted and
  // explicitly pending native-speaker review (issue #209 requirement) — it
  // must not be reachable by a real user through any path, not just absent
  // from the switcher's own options.
  it("does not restore a stored zh-Hant locale (not yet human-reviewed) — falls back to the default", async () => {
    withLocaleStorage("zh-Hant");

    render(
      <LocaleProvider>
        <p>content</p>
      </LocaleProvider>,
    );

    await waitFor(() => expect(document.documentElement.lang).toBe("en"));
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
      <LocaleProvider>
        <p>content</p>
      </LocaleProvider>,
    );

    await waitFor(() => expect(document.documentElement.lang).toBe("zh-Hans"));
    expect(window.localStorage.getItem("portfonia:locale")).toBe("zh-Hans");
  });
});
