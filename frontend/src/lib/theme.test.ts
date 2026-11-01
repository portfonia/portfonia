import { expect, it, vi } from "vitest";
import { THEME_INIT_SCRIPT } from "./theme";

it("856 A1 initializes each stored preference and error case before paint", () => {
  const cases = [
    [null, true, false], [null, false, true], ["dark", true, true],
    ["light", false, false], ["junk", true, false],
    ["storage-error", true, true], [null, "media-error", true],
  ] as const;
  for (const [stored, osLight, dark] of cases) {
    document.documentElement.className = "dark";
    vi.stubGlobal("localStorage", { getItem: () => {
      if (stored === "storage-error") throw new Error("storage unavailable");
      return stored;
    } });
    vi.stubGlobal("matchMedia", () => {
      if (osLight === "media-error") throw new Error("media unavailable");
      return { matches: osLight };
    });
    new Function(THEME_INIT_SCRIPT)();
    expect(document.documentElement.classList.contains("dark"), `${stored}/${osLight}`).toBe(dark);
  }
  vi.unstubAllGlobals();
});
