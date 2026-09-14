// @vitest-environment node
import { afterEach, describe, expect, it } from "vitest";

import { configuredVigilOrigin } from "./vigil-origin";

const ORIGINAL = process.env.NEXT_PUBLIC_VIGIL_ORIGIN;

describe("configuredVigilOrigin", () => {
  afterEach(() => {
    process.env.NEXT_PUBLIC_VIGIL_ORIGIN = ORIGINAL;
  });

  it("defaults to the Vigil HTTPS origin", () => {
    delete process.env.NEXT_PUBLIC_VIGIL_ORIGIN;
    expect(configuredVigilOrigin()).toBe("https://vigil.portfonia.com");
  });

  it("returns the exact configured origin", () => {
    process.env.NEXT_PUBLIC_VIGIL_ORIGIN = "https://vigil.portfonia.com";
    expect(configuredVigilOrigin()).toBe("https://vigil.portfonia.com");
  });

  it.each([
    "http://vigil.portfonia.com",
    "https://vigil.portfonia.com/path",
    "https://vigil.portfonia.com?x=1",
    "*",
    "vigil.portfonia.com",
  ])("rejects %j", (value) => {
    process.env.NEXT_PUBLIC_VIGIL_ORIGIN = value;
    expect(() => configuredVigilOrigin()).toThrow(/NEXT_PUBLIC_VIGIL_ORIGIN/);
  });
});
