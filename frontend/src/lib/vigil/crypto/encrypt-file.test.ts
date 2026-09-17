// @vitest-environment node
//
// P2.2-A01 (=A03): "independent vectors ... for password/no-password/
// nonASCII/empty/max file; altered AAD/C rejects with no Blob output."
// These are this checkpoint's independent vectors, run against Node's own
// WebCrypto/Argon2-WASM implementations (see aes-gcm.test.ts and
// kdf.test.ts for why `node`, not jsdom). A second, distinct browser
// engine is exercised manually via the Claude Browser tool against the
// real /vigil/setup page as a smoke test — see the PR description for
// what that covered and what it didn't (no automated second-engine CI
// harness exists in this repo).
import { describe, expect, it } from "vitest";

import { decryptVigilFile, encryptVigilFile } from "./encrypt-file";
import { VigilCryptoInputError, VigilCryptoIntegrityError } from "./errors";
import { VIGIL_GCM_TAG_LENGTH, VIGIL_INNER_LENGTH_NO_PASSWORD, VIGIL_INNER_LENGTH_WITH_PASSWORD } from "./manifest";

const VAULT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const OBJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function textPlaintext(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

describe("encryptVigilFile / decryptVigilFile round trip", () => {
  it("no-password: inner is the raw 32-byte DEK, manifest has null salt/kdf/inner_nonce", async () => {
    const plaintext = textPlaintext("no password vector");
    const result = await encryptVigilFile({
      vaultId: VAULT_ID,
      objectId: OBJECT_ID,
      plaintext,
      password: null,
    });

    expect(result.manifest.has_password).toBe(false);
    expect(result.inner.length).toBe(VIGIL_INNER_LENGTH_NO_PASSWORD);
    if (!result.manifest.has_password) {
      expect(result.manifest.salt).toBeNull();
      expect(result.manifest.kdf).toBeNull();
      expect(result.manifest.inner_nonce).toBeNull();
    }
    expect(result.ciphertext.length).toBe(plaintext.length + VIGIL_GCM_TAG_LENGTH);

    const recovered = await decryptVigilFile({
      manifest: result.manifest,
      inner: result.inner,
      ciphertext: result.ciphertext,
    });
    expect(recovered).toEqual(plaintext);
  });

  it("with password (ASCII): inner is 48 bytes, manifest carries the exact KDF contract", async () => {
    const plaintext = textPlaintext("password vector");
    const password = textPlaintext("correct horse battery staple");
    const result = await encryptVigilFile({
      vaultId: VAULT_ID,
      objectId: OBJECT_ID,
      plaintext,
      password,
    });

    expect(result.manifest.has_password).toBe(true);
    expect(result.inner.length).toBe(VIGIL_INNER_LENGTH_WITH_PASSWORD);
    if (result.manifest.has_password) {
      expect(result.manifest.kdf).toEqual({
        name: "argon2id",
        version: 19,
        memory_kib: 65536,
        iterations: 3,
        parallelism: 1,
        length: 32,
      });
    }

    const recovered = await decryptVigilFile({
      manifest: result.manifest,
      inner: result.inner,
      ciphertext: result.ciphertext,
      password,
    });
    expect(recovered).toEqual(plaintext);
  });

  it("with password (non-ASCII UTF-8): round-trips raw unnormalized bytes", async () => {
    const plaintext = textPlaintext("non-ascii password vector");
    const password = textPlaintext("Ünïcödé パスワード 中文密码 🔒");
    const result = await encryptVigilFile({
      vaultId: VAULT_ID,
      objectId: OBJECT_ID,
      plaintext,
      password,
    });

    const recovered = await decryptVigilFile({
      manifest: result.manifest,
      inner: result.inner,
      ciphertext: result.ciphertext,
      password,
    });
    expect(recovered).toEqual(plaintext);

    // A visually-normalized variant of the same password must NOT decrypt
    // (Appendix B: raw bytes, not trimmed/normalized).
    const paddedPassword = textPlaintext("Ünïcödé パスワード 中文密码 🔒 ");
    await expect(
      decryptVigilFile({
        manifest: result.manifest,
        inner: result.inner,
        ciphertext: result.ciphertext,
        password: paddedPassword,
      }),
    ).rejects.toThrow(VigilCryptoIntegrityError);
  });

  it("empty file: plaintext 0 bytes -> C is exactly 16 bytes (tag only)", async () => {
    const plaintext = new Uint8Array(0);
    const result = await encryptVigilFile({
      vaultId: VAULT_ID,
      objectId: OBJECT_ID,
      plaintext,
      password: null,
    });
    expect(result.ciphertext.length).toBe(VIGIL_GCM_TAG_LENGTH);
    const recovered = await decryptVigilFile({
      manifest: result.manifest,
      inner: result.inner,
      ciphertext: result.ciphertext,
    });
    expect(recovered.length).toBe(0);
  });

  it("max-size file: 10,000,000-byte plaintext -> C is exactly 10,000,016 bytes", async () => {
    const plaintext = new Uint8Array(10_000_000);
    for (let i = 0; i < plaintext.length; i += 1) plaintext[i] = i % 256;
    const result = await encryptVigilFile({
      vaultId: VAULT_ID,
      objectId: OBJECT_ID,
      plaintext,
      password: null,
    });
    expect(result.ciphertext.length).toBe(10_000_016);
    const recovered = await decryptVigilFile({
      manifest: result.manifest,
      inner: result.inner,
      ciphertext: result.ciphertext,
    });
    // A plain `toEqual` deep-compares 10M-element typed arrays via
    // property enumeration and can exhaust the test worker's heap —
    // `Buffer.compare` does the same byte-for-byte check without it.
    expect(recovered.length).toBe(plaintext.length);
    expect(Buffer.compare(recovered, plaintext)).toBe(0);
  }, 30_000);

  it("rejects a password outside 1-1024 bytes with no ciphertext produced", async () => {
    await expect(
      encryptVigilFile({
        vaultId: VAULT_ID,
        objectId: OBJECT_ID,
        plaintext: textPlaintext("x"),
        password: new Uint8Array(0),
      }),
    ).rejects.toThrow(VigilCryptoInputError);

    await expect(
      encryptVigilFile({
        vaultId: VAULT_ID,
        objectId: OBJECT_ID,
        plaintext: textPlaintext("x"),
        password: new Uint8Array(1025).fill(0x61),
      }),
    ).rejects.toThrow(VigilCryptoInputError);
  });
});

describe("tamper rejection (P2.2-A01: altered AAD or ciphertext byte is rejected, no Blob)", () => {
  it("rejects when a ciphertext byte is flipped after encryption", async () => {
    const plaintext = textPlaintext("tamper target");
    const result = await encryptVigilFile({
      vaultId: VAULT_ID,
      objectId: OBJECT_ID,
      plaintext,
      password: null,
    });
    const tamperedCiphertext = new Uint8Array(result.ciphertext);
    tamperedCiphertext[0] ^= 0x01;

    await expect(
      decryptVigilFile({ manifest: result.manifest, inner: result.inner, ciphertext: tamperedCiphertext }),
    ).rejects.toThrow(VigilCryptoIntegrityError);
  });

  it("rejects when the manifest's object_id (part of the AAD) is altered", async () => {
    const plaintext = textPlaintext("aad tamper target");
    const result = await encryptVigilFile({
      vaultId: VAULT_ID,
      objectId: OBJECT_ID,
      plaintext,
      password: null,
    });
    const tamperedManifest = { ...result.manifest, object_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc" };

    await expect(
      decryptVigilFile({ manifest: tamperedManifest, inner: result.inner, ciphertext: result.ciphertext }),
    ).rejects.toThrow(VigilCryptoIntegrityError);
  });

  it("rejects when the inner (wrapped DEK) is tampered under a password", async () => {
    const plaintext = textPlaintext("inner tamper target");
    const password = textPlaintext("hunter2");
    const result = await encryptVigilFile({
      vaultId: VAULT_ID,
      objectId: OBJECT_ID,
      plaintext,
      password,
    });
    const tamperedInner = new Uint8Array(result.inner);
    tamperedInner[0] ^= 0x01;

    await expect(
      decryptVigilFile({
        manifest: result.manifest,
        inner: tamperedInner,
        ciphertext: result.ciphertext,
        password,
      }),
    ).rejects.toThrow(VigilCryptoIntegrityError);
  });

  it("rejects with the wrong password entirely", async () => {
    const plaintext = textPlaintext("wrong password target");
    const result = await encryptVigilFile({
      vaultId: VAULT_ID,
      objectId: OBJECT_ID,
      plaintext,
      password: textPlaintext("right password"),
    });

    await expect(
      decryptVigilFile({
        manifest: result.manifest,
        inner: result.inner,
        ciphertext: result.ciphertext,
        password: textPlaintext("wrong password"),
      }),
    ).rejects.toThrow(VigilCryptoIntegrityError);
  });
});

describe("retry / regeneration semantics (P2.2-A04/=A05)", () => {
  it("two calls with the same inputs produce different DEK/nonces/salt (this function always mints fresh material)", async () => {
    const plaintext = textPlaintext("same input, different vault_id-scoped object");
    const password = textPlaintext("same password");
    const first = await encryptVigilFile({ vaultId: VAULT_ID, objectId: OBJECT_ID, plaintext, password });
    const second = await encryptVigilFile({ vaultId: VAULT_ID, objectId: OBJECT_ID, plaintext, password });

    // Fresh DEK/nonces/salt each call -> different ciphertext and inner
    // even for byte-identical plaintext+password. A plain "retry" in the
    // product UI must NOT call this function again; it must resend the
    // previously-produced {manifest, inner, ciphertext} unchanged — that
    // policy lives in the caller (worker-client.ts), not here.
    expect(first.ciphertext).not.toEqual(second.ciphertext);
    expect(first.inner).not.toEqual(second.inner);
    expect(first.manifest.file_nonce).not.toEqual(second.manifest.file_nonce);
  });
});
