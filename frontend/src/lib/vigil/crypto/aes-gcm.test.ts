// @vitest-environment node
//
// jsdom's window.crypto has no `.subtle` (SubtleCrypto is unimplemented in
// jsdom as of this repo's pinned version) — this module is exercised
// against Node's own spec-compliant WebCrypto implementation instead,
// which is the same W3C SubtleCrypto surface a real browser Worker uses.
import { describe, expect, it } from "vitest";

import { aesGcmDecrypt, aesGcmEncrypt, randomBytes } from "./aes-gcm";
import { VIGIL_GCM_TAG_LENGTH } from "./manifest";

const KEY = new Uint8Array(32).map((_, i) => i);
const NONCE = new Uint8Array(12).map((_, i) => 100 + i);
const AAD = new TextEncoder().encode(`["vigil-file",1,"vault","object"]`);

describe("aesGcmEncrypt / aesGcmDecrypt", () => {
  it("round-trips empty plaintext to a 16-byte ciphertext (tag only)", async () => {
    const plaintext = new Uint8Array(0);
    const c = await aesGcmEncrypt(KEY, NONCE, plaintext, AAD);
    expect(c.length).toBe(VIGIL_GCM_TAG_LENGTH);
    const recovered = await aesGcmDecrypt(KEY, NONCE, c, AAD);
    expect(recovered).toEqual(plaintext);
  });

  it("round-trips non-empty plaintext, C = ciphertext||tag (plaintext length + 16)", async () => {
    const plaintext = new TextEncoder().encode("the quick brown fox jumps over the lazy dog");
    const c = await aesGcmEncrypt(KEY, NONCE, plaintext, AAD);
    expect(c.length).toBe(plaintext.length + VIGIL_GCM_TAG_LENGTH);
    const recovered = await aesGcmDecrypt(KEY, NONCE, c, AAD);
    expect(recovered).toEqual(plaintext);
  });

  it("rejects decryption when a single ciphertext byte is altered (tamper -> reject, no output)", async () => {
    const plaintext = new TextEncoder().encode("do not tamper with me");
    const c = await aesGcmEncrypt(KEY, NONCE, plaintext, AAD);
    const tampered = new Uint8Array(c);
    tampered[0] ^= 0x01;
    await expect(aesGcmDecrypt(KEY, NONCE, tampered, AAD)).rejects.toThrow();
  });

  it("rejects decryption when the AAD is altered", async () => {
    const plaintext = new TextEncoder().encode("bind me to my row");
    const c = await aesGcmEncrypt(KEY, NONCE, plaintext, AAD);
    const wrongAad = new TextEncoder().encode(`["vigil-file",1,"vault","other-object"]`);
    await expect(aesGcmDecrypt(KEY, NONCE, c, wrongAad)).rejects.toThrow();
  });

  it("rejects decryption when the tag (last 16 bytes) is altered", async () => {
    const plaintext = new TextEncoder().encode("check the tag too");
    const c = await aesGcmEncrypt(KEY, NONCE, plaintext, AAD);
    const tampered = new Uint8Array(c);
    tampered[tampered.length - 1] ^= 0x01;
    await expect(aesGcmDecrypt(KEY, NONCE, tampered, AAD)).rejects.toThrow();
  });

  it("produces different ciphertext for different nonces (no nonce reuse in these vectors)", async () => {
    const plaintext = new TextEncoder().encode("same plaintext");
    const nonceB = new Uint8Array(12).map((_, i) => 200 + i);
    const c1 = await aesGcmEncrypt(KEY, NONCE, plaintext, AAD);
    const c2 = await aesGcmEncrypt(KEY, nonceB, plaintext, AAD);
    expect(c1).not.toEqual(c2);
  });
});

describe("randomBytes", () => {
  it("returns the requested length", () => {
    expect(randomBytes(32).length).toBe(32);
    expect(randomBytes(12).length).toBe(12);
    expect(randomBytes(16).length).toBe(16);
  });

  it("does not return the same bytes twice in a row (no fixed/zero fallback)", () => {
    const a = randomBytes(32);
    const b = randomBytes(32);
    expect(a).not.toEqual(b);
  });
});
