import { describe, expect, it } from "vitest";

import { decodeBase64Url, encodeBase64Url } from "./base64url";

describe("encodeBase64Url", () => {
  it("encodes without padding and using URL-safe alphabet", () => {
    // 12 zero bytes would base64-pad to "AAAAAAAAAAAAAAAA" (16 chars, no
    // padding needed) — pick lengths that actually produce '=' padding in
    // standard base64 to prove it's stripped.
    const bytes = new Uint8Array([0xff, 0xef, 0xbe]);
    expect(encodeBase64Url(bytes)).toBe("_---");
  });

  it("produces no padding characters for any input length", () => {
    for (let len = 0; len < 20; len += 1) {
      const bytes = new Uint8Array(len).map((_, i) => i);
      expect(encodeBase64Url(bytes)).not.toContain("=");
      expect(encodeBase64Url(bytes)).not.toContain("+");
      expect(encodeBase64Url(bytes)).not.toContain("/");
    }
  });

  it("round-trips through decodeBase64Url for random-like byte sequences", () => {
    const vectors = [
      new Uint8Array(0),
      new Uint8Array([0]),
      new Uint8Array(Array.from({ length: 12 }, (_, i) => i * 7 + 1)),
      new Uint8Array(Array.from({ length: 32 }, (_, i) => (i * 31) % 256)),
      new Uint8Array(Array.from({ length: 48 }, (_, i) => (255 - i) % 256)),
    ];
    for (const bytes of vectors) {
      expect(decodeBase64Url(encodeBase64Url(bytes))).toEqual(bytes);
    }
  });
});

describe("decodeBase64Url", () => {
  it("rejects strings containing standard-base64 padding or alphabet characters", () => {
    expect(() => decodeBase64Url("AAA=")).toThrow();
    expect(() => decodeBase64Url("++++")).toThrow();
    expect(() => decodeBase64Url("////")).toThrow();
  });
});
