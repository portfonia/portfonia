import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

// RangeTabs imports PERFORMANCE_RANGES from @/lib/api, which imports
// @/lib/auth-actions -> lib/supabase/server's `server-only` guard — the
// same import chain every lib/api consumer mocks in tests.
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { RangeTabs } from "./range-tabs";

function renderTabs(onChange = vi.fn()) {
  render(
    <LocaleProvider>
      <RangeTabs value="1Y" onChange={onChange} />
    </LocaleProvider>,
  );
  return onChange;
}

describe("RangeTabs", () => {
  it("offers exactly 1M/6M/YTD/1Y/5Y/ALL and marks the current one pressed", () => {
    renderTabs();

    const group = screen.getByRole("radiogroup", { name: "Time range" });
    expect(group).toBeInTheDocument();
    for (const label of ["1M", "6M", "YTD", "1Y", "5Y", "All"]) {
      const button = screen.getByRole("button", { name: label });
      expect(button).toHaveAttribute("aria-pressed", label === "1Y" ? "true" : "false");
    }
    expect(screen.queryByRole("button", { name: "1D" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "5D" })).not.toBeInTheDocument();
  });

  it("calls onChange with the clicked range", async () => {
    const user = userEvent.setup();
    const onChange = renderTabs();

    await user.click(screen.getByRole("button", { name: "6M" }));

    expect(onChange).toHaveBeenCalledWith("6M");
  });

  it("renders translated range labels under zh-Hans", async () => {
    const store = new Map<string, string>([["portfonia:locale", "zh-Hans"]]);
    Object.defineProperty(window, "localStorage", {
      value: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => void store.set(key, value),
        removeItem: (key: string) => void store.delete(key),
        clear: () => store.clear(),
      },
      configurable: true,
    });

    render(
      <LocaleProvider>
        <RangeTabs value="ALL" onChange={vi.fn()} />
      </LocaleProvider>,
    );

    expect(await screen.findByRole("button", { name: "全部" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});
