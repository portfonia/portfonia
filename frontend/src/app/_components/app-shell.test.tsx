import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppShell } from "./app-shell";

const { useLocale } = vi.hoisted(() => ({ useLocale: vi.fn() }));

vi.mock("./locale-provider", () => ({ useLocale }));

describe("AppShell", () => {
  it.each(["en", "zh-Hans", "zh-Hant"] as const)(
    "sets lang=%s to match the selected locale on every route (issue #209: home-only gate removed)",
    (locale) => {
      useLocale.mockReturnValue({ locale, setLocale: vi.fn() });

      const { container } = render(
        <AppShell>
          <p>content</p>
        </AppShell>,
      );

      expect(container.firstElementChild).toHaveAttribute("lang", locale);
    },
  );
});
