import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { THEME_STORAGE_KEY } from "@/lib/theme";
import { useThemePreference } from "./use-theme-preference";

let osLight = false;
let listeners: Set<() => void>;
let store: Map<string, string>;
beforeEach(() => {
  store = new Map(); listeners = new Set(); osLight = false;
  document.documentElement.className = "dark";
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => store.set(key, value),
    removeItem: (key: string) => store.delete(key),
  });
  vi.stubGlobal("matchMedia", () => ({ matches: osLight,
    addEventListener: (_: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
  }));
});
afterEach(() => vi.unstubAllGlobals());

it("856 A2 stores explicit choices and removes the key for system", () => {
  const { result } = renderHook(useThemePreference);
  act(() => result.current.setPreference("light"));
  expect([store.get(THEME_STORAGE_KEY), document.documentElement.classList.contains("dark"), result.current.preference]).toEqual(["light", false, "light"]);
  act(() => result.current.setPreference("dark"));
  expect([store.get(THEME_STORAGE_KEY), document.documentElement.classList.contains("dark")]).toEqual(["dark", true]);
  osLight = true;
  act(() => result.current.setPreference("system"));
  expect([store.has(THEME_STORAGE_KEY), document.documentElement.classList.contains("dark")]).toEqual([false, false]);
});

it("856 A3 reapplies on OS changes only while following system", () => {
  const { result, unmount } = renderHook(useThemePreference);
  osLight = true;
  act(() => listeners.forEach(listener => listener()));
  expect(document.documentElement.classList.contains("dark")).toBe(false);
  act(() => result.current.setPreference("light"));
  osLight = false;
  act(() => listeners.forEach(listener => listener()));
  expect(document.documentElement.classList.contains("dark")).toBe(false);
  expect(listeners.size).toBe(0);
  act(() => result.current.setPreference("system"));
  expect(listeners.size).toBe(1);
  unmount();
  expect(listeners.size).toBe(0);
});
