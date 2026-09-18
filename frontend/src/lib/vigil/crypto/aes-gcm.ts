// AES-256-GCM primitives on the standard W3C SubtleCrypto interface
// (available identically in a browser Worker and in Node). Browser v1 contract, #450 Design section 5:
// "C = ciphertext||tag" with a 16-byte tag — SubtleCrypto's `encrypt`
// already appends the tag to its output and `decrypt` requires it appended
// on input, so no manual concatenation is needed here.
import { VigilCryptoIntegrityError, VigilCryptoUnavailableError } from "./errors";

const GCM_TAG_BITS = 128;

function subtle(): SubtleCrypto {
  const impl = globalThis.crypto?.subtle;
  if (!impl) {
    throw new VigilCryptoUnavailableError(
      "Web Crypto (SubtleCrypto) is not available in this browser",
    );
  }
  return impl;
}

export function randomBytes(length: number): Uint8Array {
  const impl = globalThis.crypto;
  if (!impl?.getRandomValues) {
    throw new VigilCryptoUnavailableError(
      "Web Crypto (getRandomValues) is not available in this browser",
    );
  }
  return impl.getRandomValues(new Uint8Array(length));
}

// TS 5.7+ made `Uint8Array` generic over its backing buffer, and DOM's
// `BufferSource` narrowed to exclude `SharedArrayBuffer`-backed views. A
// `TextEncoder().encode()` result or a decoded base64url `Uint8Array` is
// typed as the wider `Uint8Array<ArrayBufferLike>` even though nothing in
// this module ever constructs one over a `SharedArrayBuffer` — this
// re-copy makes that true at the type level at the one place it matters
// (the SubtleCrypto call boundary) instead of forcing every caller in the
// module to track it.
function asBufferSource(bytes: Uint8Array): Uint8Array<ArrayBuffer> {
  return new Uint8Array(bytes);
}

async function importAesGcmKey(rawKey: Uint8Array, usage: KeyUsage): Promise<CryptoKey> {
  try {
    return await subtle().importKey("raw", asBufferSource(rawKey), "AES-GCM", false, [usage]);
  } catch (cause) {
    throw new VigilCryptoUnavailableError("failed to import AES-GCM key material", { cause });
  }
}

export async function aesGcmEncrypt(
  key: Uint8Array,
  nonce: Uint8Array,
  plaintext: Uint8Array,
  aad: Uint8Array,
): Promise<Uint8Array> {
  const cryptoKey = await importAesGcmKey(key, "encrypt");
  try {
    const result = await subtle().encrypt(
      {
        name: "AES-GCM",
        iv: asBufferSource(nonce),
        additionalData: asBufferSource(aad),
        tagLength: GCM_TAG_BITS,
      },
      cryptoKey,
      asBufferSource(plaintext),
    );
    return new Uint8Array(result);
  } catch (cause) {
    throw new VigilCryptoUnavailableError("AES-GCM encryption failed", { cause });
  }
}

// Rejects (throws) on any tampered ciphertext, tag, nonce mismatch, or AAD
// mismatch — this is SubtleCrypto's own authenticated-decryption failure
// mode, not something this wrapper re-implements. No plaintext, partial or
// otherwise, is ever returned on a failed verification.
export async function aesGcmDecrypt(
  key: Uint8Array,
  nonce: Uint8Array,
  ciphertext: Uint8Array,
  aad: Uint8Array,
): Promise<Uint8Array> {
  const cryptoKey = await importAesGcmKey(key, "decrypt");
  try {
    const result = await subtle().decrypt(
      {
        name: "AES-GCM",
        iv: asBufferSource(nonce),
        additionalData: asBufferSource(aad),
        tagLength: GCM_TAG_BITS,
      },
      cryptoKey,
      asBufferSource(ciphertext),
    );
    return new Uint8Array(result);
  } catch (cause) {
    throw new VigilCryptoIntegrityError(
      "AES-GCM authentication failed: ciphertext, tag, or AAD does not match",
      { cause },
    );
  }
}
