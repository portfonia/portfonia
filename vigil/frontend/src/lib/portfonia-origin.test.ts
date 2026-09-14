// @vitest-environment node
import { afterEach, describe, expect, it } from "vitest";

import { configuredPortfoniaOrigin } from "./portfonia-origin";

const ORIGINAL = process.env.NEXT_PUBLIC_PORTFONIA_ORIGIN;

describe("configuredPortfoniaOrigin", () => {
  afterEach(() => {
    process.env.NEXT_PUBLIC_PORTFONIA_ORIGIN = ORIGINAL;
  });

  it("defaults to https://portfonia.com", () => {
    delete process.env.NEXT_PUBLIC_PORTFONIA_ORIGIN;
    expect(configuredPortfoniaOrigin()).toBe("https://portfonia.com");
  });

  it.each(["http://portfonia.com", "https://portfonia.com/auth/vigil", "*"])(
    "rejects %j",
    (value) => {
      process.env.NEXT_PUBLIC_PORTFONIA_ORIGIN = value;
      expect(() => configuredPortfoniaOrigin()).toThrow(/NEXT_PUBLIC_PORTFONIA_ORIGIN/);
    },
  );
});
