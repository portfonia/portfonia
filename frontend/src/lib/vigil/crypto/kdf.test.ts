import { describe, expect, it } from "vitest";

import { deriveArgon2idKey } from "./kdf";

function hexToBytes(hex: string): Uint8Array {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i += 1) {
    bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// Independent known-answer vectors: generated with argon2-cffi
// (`hash_secret_raw`, Type.ID, version=19) at this contract's exact
// parameters (m=65536 KiB, t=3, p=1, out=32) — a second implementation,
// not this module's own output, per P2.2-A01's "independent vectors"
// requirement. See the PR description for the exact generating command.
const KNOWN_ANSWER_VECTORS = [
  {
    name: "ASCII password",
    passwordHex: "636f727265637420686f727365206261747465727920737461706c65",
    saltHex: "30313233343536373839616263646566",
    outputHex: "94c86f541abdb3d9aabfea59aa03963549483e9c0b1a79336e76b54cee6c917e",
  },
  {
    name: "non-ASCII UTF-8 password",
    passwordHex: "c3bcc3b6c3a4206e6f6e2d617363696920707720e4b8ade69687",
    saltHex: "000102030405060708090a0b0c0d0e0f",
    outputHex: "939dc4b183b389b8fcfc057eaafac533d46467b3d8b1208cb2c0556fc5becd56",
  },
  {
    name: "1-byte (minimum-length) password",
    passwordHex: "78",
    saltHex: "ffffffffffffffffffffffffffffffff",
    outputHex: "209bee68b5fb48a66d1e63aa986263841735a9e43d6b89e97af2b4981ae993fc",
  },
];

describe("deriveArgon2idKey", () => {
  it.each(KNOWN_ANSWER_VECTORS)(
    "matches the independent Argon2id($name) known-answer vector",
    async ({ passwordHex, saltHex, outputHex }) => {
      const derived = await deriveArgon2idKey(hexToBytes(passwordHex), hexToBytes(saltHex));
      expect(bytesToHex(derived)).toBe(outputHex);
    },
  );

  it("returns exactly 32 bytes", async () => {
    const derived = await deriveArgon2idKey(new TextEncoder().encode("password"), new Uint8Array(16));
    expect(derived.length).toBe(32);
  });

  it("produces a different key for a different salt (same password)", async () => {
    const password = new TextEncoder().encode("same password");
    const a = await deriveArgon2idKey(password, new Uint8Array(16).fill(1));
    const b = await deriveArgon2idKey(password, new Uint8Array(16).fill(2));
    expect(a).not.toEqual(b);
  });

  it("produces a different key for a different password (same salt)", async () => {
    const salt = new Uint8Array(16).fill(7);
    const a = await deriveArgon2idKey(new TextEncoder().encode("password-a"), salt);
    const b = await deriveArgon2idKey(new TextEncoder().encode("password-b"), salt);
    expect(a).not.toEqual(b);
  });

  it("does not trim or normalize the raw password bytes (Appendix B: unnormalized/untrimmed)", async () => {
    const salt = new Uint8Array(16).fill(9);
    const untrimmed = await deriveArgon2idKey(new TextEncoder().encode("  password  "), salt);
    const trimmed = await deriveArgon2idKey(new TextEncoder().encode("password"), salt);
    expect(untrimmed).not.toEqual(trimmed);
  });
}, 20_000);
