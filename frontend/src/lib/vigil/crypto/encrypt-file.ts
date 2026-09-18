// Orchestrates one Browser-v1 encryption pass (#450 Design section 5
// #450 Design section 5's "Browser v1" contract). Pure/stateless: given (vaultId, objectId, plaintext,
// password) it always produces FRESH random DEK/nonces/salt — callers
// (the Worker, and above it the setup-page hook) are responsible for
// caching the result and only calling this again when the file or
// password actually changes, never on a plain retry (issue #455 scope:
// "retry reuses the same already-created ciphertext").
import { aesGcmDecrypt, aesGcmEncrypt, randomBytes } from "./aes-gcm";
import { buildFileAad, buildInnerAad } from "./aad";
import { decodeBase64Url, encodeBase64Url } from "./base64url";
import { VigilCryptoInputError } from "./errors";
import { deriveArgon2idKey } from "./kdf";
import {
  VIGIL_DEK_LENGTH,
  VIGIL_FILE_NONCE_LENGTH,
  VIGIL_INNER_NONCE_LENGTH,
  VIGIL_KDF_PARAMS,
  VIGIL_MANIFEST_ALGORITHM,
  VIGIL_MANIFEST_VERSION,
  VIGIL_MAX_PASSWORD_BYTES,
  VIGIL_MIN_PASSWORD_BYTES,
  VIGIL_SALT_LENGTH,
  type VigilManifest,
} from "./manifest";

export interface EncryptVigilFileInput {
  vaultId: string;
  objectId: string;
  plaintext: Uint8Array;
  /** Raw UTF-8 password bytes, not trimmed/normalized, or `null` for the
   * explicit "no password" choice (Browser v1 contract: "Without password
   * explicitly chosen, inner=DEK"). */
  password: Uint8Array | null;
}

export interface EncryptVigilFileResult {
  manifest: VigilManifest;
  /** 32 bytes (no password) or 48 bytes (password) — sent to the backend
   * as the unpadded-base64url `inner` multipart field. */
  inner: Uint8Array;
  /** C = ciphertext||tag — sent as the `file` multipart field. */
  ciphertext: Uint8Array;
}

export async function encryptVigilFile(input: EncryptVigilFileInput): Promise<EncryptVigilFileResult> {
  const dek = randomBytes(VIGIL_DEK_LENGTH);
  const fileNonce = randomBytes(VIGIL_FILE_NONCE_LENGTH);
  const ciphertext = await aesGcmEncrypt(
    dek,
    fileNonce,
    input.plaintext,
    buildFileAad(input.vaultId, input.objectId),
  );

  if (input.password === null) {
    return {
      manifest: {
        version: VIGIL_MANIFEST_VERSION,
        algorithm: VIGIL_MANIFEST_ALGORITHM,
        vault_id: input.vaultId,
        object_id: input.objectId,
        has_password: false,
        file_nonce: encodeBase64Url(fileNonce),
        salt: null,
        kdf: null,
        inner_nonce: null,
      },
      inner: dek,
      ciphertext,
    };
  }

  if (input.password.length < VIGIL_MIN_PASSWORD_BYTES || input.password.length > VIGIL_MAX_PASSWORD_BYTES) {
    throw new VigilCryptoInputError(
      `password must be ${VIGIL_MIN_PASSWORD_BYTES}-${VIGIL_MAX_PASSWORD_BYTES} UTF-8 bytes`,
    );
  }

  const salt = randomBytes(VIGIL_SALT_LENGTH);
  const innerNonce = randomBytes(VIGIL_INNER_NONCE_LENGTH);
  const derivedKey = await deriveArgon2idKey(input.password, salt);
  const inner = await aesGcmEncrypt(
    derivedKey,
    innerNonce,
    dek,
    buildInnerAad(input.vaultId, input.objectId),
  );

  return {
    manifest: {
      version: VIGIL_MANIFEST_VERSION,
      algorithm: VIGIL_MANIFEST_ALGORITHM,
      vault_id: input.vaultId,
      object_id: input.objectId,
      has_password: true,
      file_nonce: encodeBase64Url(fileNonce),
      salt: encodeBase64Url(salt),
      kdf: VIGIL_KDF_PARAMS,
      inner_nonce: encodeBase64Url(innerNonce),
    },
    inner,
    ciphertext,
  };
}

export interface DecryptVigilFileInput {
  manifest: VigilManifest;
  inner: Uint8Array;
  ciphertext: Uint8Array;
  /** Required, and only used, when `manifest.has_password` is true. */
  password?: Uint8Array;
}

// Verification-only counterpart of `encryptVigilFile`: exercised by this
// checkpoint's own test vectors (P2.2-A01 "tampered AAD/ciphertext yields
// no download") and by future retrieval work (#461), never called from
// the setup-page UI shipped in this checkpoint.
export async function decryptVigilFile(input: DecryptVigilFileInput): Promise<Uint8Array> {
  const { manifest } = input;
  let dek: Uint8Array;
  if (manifest.has_password) {
    if (!input.password) {
      throw new VigilCryptoInputError("password is required to decrypt this file");
    }
    const salt = decodeBase64Url(manifest.salt);
    const innerNonce = decodeBase64Url(manifest.inner_nonce);
    const derivedKey = await deriveArgon2idKey(input.password, salt);
    dek = await aesGcmDecrypt(
      derivedKey,
      innerNonce,
      input.inner,
      buildInnerAad(manifest.vault_id, manifest.object_id),
    );
  } else {
    dek = input.inner;
  }

  const fileNonce = decodeBase64Url(manifest.file_nonce);
  return aesGcmDecrypt(
    dek,
    fileNonce,
    input.ciphertext,
    buildFileAad(manifest.vault_id, manifest.object_id),
  );
}
