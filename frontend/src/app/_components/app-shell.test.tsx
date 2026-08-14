import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppShell } from "./app-shell";

const { usePathname } = vi.hoisted(() => ({ usePathname: vi.fn() }));
const { useLocale } = vi.hoisted(() => ({ useLocale: vi.fn() }));

vi.mock("next/navigation", () => ({ usePathname }));
vi.mock("./locale-provider", () => ({ useLocale }));

describe("AppShell", () => {
  it("keeps lang=en on non-home routes even when the stored locale is zh", () => {
    usePathname.mockReturnValue("/holdings");
    useLocale.mockReturnValue({ locale: "zh", setLocale: vi.fn() });

    const { container } = render(
      <AppShell>
        <p>content</p>
      </AppShell>,
    );

    expect(container.firstElementChild).toHaveAttribute("lang", "en");
  });

  it("follows the selected locale on the home route", () => {
    usePathname.mockReturnValue("/");
    useLocale.mockReturnValue({ locale: "zh", setLocale: vi.fn() });

    const { container } = render(
      <AppShell>
        <p>content</p>
      </AppShell>,
    );

    expect(container.firstElementChild).toHaveAttribute("lang", "zh");
  });
});
