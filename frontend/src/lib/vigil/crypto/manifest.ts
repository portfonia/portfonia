// Exact "Browser v1" manifest/KDF shape, #450 Design section 5:
//   kdf = {name:'argon2id', version:19, memory_kib:65536, iterations:3,
//          parallelism:1, length:32}
// This object is sent byte-for-byte as JSON and re-validated by the
// backend (`_REQUIRED_KDF` in `backend/app/services/vigil/objects.py`) —
// any drift between the two is a 422 on upload, so the two must be kept
// identical by inspection, not by a shared source (different languages).

export const VIGIL_KDF_PARAMS = {
  name: "argon2id",
  version: 19,
  memory_kib: 65536,
  iterations: 3,
  parallelism: 1,
  length: 32,
} as const;

export type VigilKdfParams = typeof VIGIL_KDF_PARAMS;

export const VIGIL_MANIFEST_VERSION = 1;
export const VIGIL_MANIFEST_ALGORITHM = "AES-256-GCM";

export const VIGIL_DEK_LENGTH = 32;
export const VIGIL_FILE_NONCE_LENGTH = 12;
export const VIGIL_GCM_TAG_LENGTH = 16;
export const VIGIL_SALT_LENGTH = 16;
export const VIGIL_INNER_NONCE_LENGTH = 12;
export const VIGIL_INNER_LENGTH_NO_PASSWORD = VIGIL_DEK_LENGTH;
export const VIGIL_INNER_LENGTH_WITH_PASSWORD = VIGIL_DEK_LENGTH + VIGIL_GCM_TAG_LENGTH;
export const VIGIL_MIN_PASSWORD_BYTES = 1;
export const VIGIL_MAX_PASSWORD_BYTES = 1024;

export interface VigilManifestNoPassword {
  version: typeof VIGIL_MANIFEST_VERSION;
  algorithm: typeof VIGIL_MANIFEST_ALGORITHM;
  vault_id: string;
  object_id: string;
  has_password: false;
  file_nonce: string;
  salt: null;
  kdf: null;
  inner_nonce: null;
}

export interface VigilManifestWithPassword {
  version: typeof VIGIL_MANIFEST_VERSION;
  algorithm: typeof VIGIL_MANIFEST_ALGORITHM;
  vault_id: string;
  object_id: string;
  has_password: true;
  file_nonce: string;
  salt: string;
  kdf: VigilKdfParams;
  inner_nonce: string;
}

export type VigilManifest = VigilManifestNoPassword | VigilManifestWithPassword;
