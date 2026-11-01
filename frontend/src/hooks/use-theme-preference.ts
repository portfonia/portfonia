"use client";

import { useCallback, useEffect, useState } from "react";
import { applyThemePreference, readThemePreference, THEME_STORAGE_KEY, type ThemePreference } from "@/lib/theme";

export function useThemePreference() {
  const [preference, setPreferenceState] = useState<ThemePreference>("system");
  const restoreStoredPreference = useCallback(() => {
    setPreferenceState(readThemePreference());
  }, []);

  useEffect(() => {
    // Restore the client-only preference once after the server render.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    restoreStoredPreference();
  }, [restoreStoredPreference]);

  useEffect(() => {
    if (preference !== "system") return;
    const media = matchMedia("(prefers-color-scheme: light)");
    const onChange = () => applyThemePreference("system");
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [preference]);

  function setPreference(next: ThemePreference) {
    try {
      if (next === "system") localStorage.removeItem(THEME_STORAGE_KEY);
      else localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // The selection still applies to this page when storage is unavailable.
    }
    applyThemePreference(next);
    setPreferenceState(next);
  }

  return { preference, setPreference };
}
