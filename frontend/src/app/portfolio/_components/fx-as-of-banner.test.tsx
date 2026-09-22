import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { FxAsOfBanner } from "./fx-as-of-banner";

function renderBanner(fxRatesAsOf: Record<string, string>, staleFxPairs: string[] = []) {
  return render(
    <LocaleProvider>
      <FxAsOfBanner fxRatesAsOf={fxRatesAsOf} staleFxPairs={staleFxPairs} />
    </LocaleProvider>,
  );
}

function bannerFor(text: RegExp): HTMLElement {
  const node = screen.getByText(text);
  const banner = node.closest("div");
  if (!banner) throw new Error("FX banner not found");
  return banner;
}

describe("FxAsOfBanner", () => {
  it("renders nothing when no conversion happened (empty map)", () => {
    const { container } = renderBanner({}, ["CNY"]);
    expect(container).toBeEmptyDOMElement();
  });

  it("lists each currency with its own date, sorted", () => {
    renderBanner({ HKD: "2026-09-03", CNY: "2026-09-04" });
    expect(screen.getByText(/CNY as of 2026-09-04/)).toBeInTheDocument();
    expect(screen.getByText(/HKD as of 2026-09-03/)).toBeInTheDocument();
  });

  it("shows different dates for different currencies without collapsing them (issue #354)", () => {
    renderBanner({ HKD: "2026-09-03", CNY: "2026-09-04" });
    const text = screen.getByText(/CNY as of/).textContent ?? "";
    expect(text).toContain("2026-09-03");
    expect(text).toContain("2026-09-04");
  });

  it("keeps the neutral banner when every displayed rate is fresh (issue #532)", () => {
    renderBanner({ HKD: "2026-09-20", CNY: "2026-09-20" }, []);
    const banner = bannerFor(/FX rates:/);
    const text = banner.textContent ?? "";
    expect(text.indexOf("CNY")).toBeLessThan(text.indexOf("HKD"));
    expect(text).toContain("CNY as of 2026-09-20");
    expect(text).toContain("HKD as of 2026-09-20");
    expect(text).not.toMatch(/more than 48 hours old/);
    expect(banner.querySelector("svg")).toBeNull();
    expect(banner).toHaveClass("border-input");
  });

  it("labels only the stale displayed currency and keeps valuation disclosure (issue #532)", () => {
    const { container } = renderBanner({ HKD: "2026-09-20", CNY: "2026-09-20" }, ["CNY"]);
    const banner = bannerFor(/Valuation still uses the latest available rates/);
    const text = banner.textContent ?? "";
    expect(text).toMatch(/CNY as of 2026-09-20 \(more than 48 hours old\)/);
    expect(text).toContain("HKD as of 2026-09-20");
    expect(text).not.toMatch(/HKD as of 2026-09-20 \(more than 48 hours old\)/);
    expect(text).toMatch(/more than 48 hours old/);
    const icons = banner.querySelectorAll("svg");
    expect(icons).toHaveLength(1);
    expect(icons[0]).toHaveAttribute("aria-hidden", "true");
    expect(banner).toHaveClass("border-amber-300/60");
    expect(container.querySelector(".recharts-surface")).toBeNull();
    expect(container.querySelector("[stroke-dasharray]")).toBeNull();
  });

  it("uses one icon when several displayed currencies are stale (issue #532)", () => {
    renderBanner({ EUR: "2026-09-18", HKD: "2026-09-20", CNY: "2026-09-19" }, ["EUR", "HKD"]);
    const banner = bannerFor(/Valuation still uses the latest available rates/);
    const text = banner.textContent ?? "";
    expect(banner.querySelectorAll("svg")).toHaveLength(1);
    expect(text).toMatch(/CNY as of 2026-09-19/);
    expect(text).not.toMatch(/CNY as of 2026-09-19 \(more than 48 hours old\)/);
    expect(text).toMatch(/EUR as of 2026-09-18 \(more than 48 hours old\)/);
    expect(text).toMatch(/HKD as of 2026-09-20 \(more than 48 hours old\)/);
    expect(text.indexOf("CNY")).toBeLessThan(text.indexOf("EUR"));
    expect(text.indexOf("EUR")).toBeLessThan(text.indexOf("HKD"));
  });

  it("ignores a stale code that is not in the displayed FX map (issue #532)", () => {
    renderBanner({ HKD: "2026-09-20" }, ["CNY"]);
    const banner = bannerFor(/FX rates:/);
    const text = banner.textContent ?? "";
    expect(text).toContain("HKD as of 2026-09-20");
    expect(text).not.toContain("CNY");
    expect(text).not.toMatch(/more than 48 hours old/);
    expect(banner.querySelector("svg")).toBeNull();
    expect(banner).toHaveClass("border-input");
  });
});
