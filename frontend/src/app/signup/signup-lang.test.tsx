import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { setLocale } = vi.hoisted(() => ({ setLocale: vi.fn() }));

vi.mock("@/app/_components/locale-provider", () => ({
  useLocale: () => ({ setLocale }),
}));

import { SignupLang } from "./signup-lang";

describe("SignupLang", () => {
  beforeEach(() => setLocale.mockClear());

  it("applies a valid signup link locale once", () => {
    render(<SignupLang initialLang="zh-Hans" />);
    expect(setLocale).toHaveBeenCalledExactlyOnceWith("zh-Hans");
  });

  it.each(["fr", "", null])("ignores invalid or missing locale %s", (initialLang) => {
    render(<SignupLang initialLang={initialLang} />);
    expect(setLocale).not.toHaveBeenCalled();
  });
});
