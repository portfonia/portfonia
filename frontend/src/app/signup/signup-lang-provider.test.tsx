import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LocaleProvider, useLocale } from "@/app/_components/locale-provider";
import { SignupLang } from "./signup-lang";

function CurrentLocale() {
  const { locale } = useLocale();
  return <span data-testid="locale">{locale}</span>;
}

describe("signup link locale with LocaleProvider", () => {
  const values = new Map<string, string>();
  beforeEach(() => {
    values.clear();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: vi.fn((key: string) => values.get(key) ?? null),
        setItem: vi.fn((key: string, value: string) => values.set(key, value)),
      },
    });
  });

  it("overrides and persists a stored preference", () => {
    window.localStorage.setItem("portfonia:locale", "en");
    render(
      <LocaleProvider routeLocale={null}>
        <SignupLang initialLang="zh-Hant" />
        <CurrentLocale />
      </LocaleProvider>,
    );
    expect(screen.getByTestId("locale")).toHaveTextContent("zh-Hant");
    expect(window.localStorage.getItem("portfonia:locale")).toBe("zh-Hant");
  });
});
