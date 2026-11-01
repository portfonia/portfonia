export const THEME_STORAGE_KEY = "portfonia:theme";
export type ThemePreference = "dark" | "light" | "system";

export function readThemePreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return stored === "dark" || stored === "light" ? stored : "system";
  } catch {
    return "system";
  }
}

export function applyThemePreference(preference: ThemePreference): void {
  const isLight = preference === "light" ||
    (preference === "system" && matchMedia("(prefers-color-scheme: light)").matches);
  document.documentElement.classList.toggle("dark", !isLight);
}

export const THEME_INIT_SCRIPT = `try {
  var preference = localStorage.getItem("${THEME_STORAGE_KEY}");
  var isLight = preference === "light" ||
    (preference !== "dark" && matchMedia("(prefers-color-scheme: light)").matches);
  document.documentElement.classList.toggle("dark", !isLight);
} catch {}`;
