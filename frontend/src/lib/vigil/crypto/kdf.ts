// Argon2id key derivation via hash-wasm — a pinned, self-hosted WASM
// build (the .wasm bytes are compiled into hash-wasm's own JS bundle as
// inline base64, so there is no `fetch()`/CDN request for the binary at
// all, at build time or runtime: stronger than "same-origin static
// asset"). Exact "Browser v1" contract, #450 Design section 5: Argon2id
// v19, memory=65536 KiB, iterations=3, parallelism=1, output=32 bytes.
//
// Never import holdings' `encrypt_value`-equivalent or any other KDF here
// as a "fallback" — an unsupported/out-of-memory environment must fail
// loudly (VigilCryptoUnavailableError), never silently derive a weaker key.
import { argon2id } from "hash-wasm";

import { VigilCryptoUnavailableError } from "./errors";
import { VIGIL_KDF_PARAMS } from "./manifest";

export async function deriveArgon2idKey(password: Uint8Array, salt: Uint8Array): Promise<Uint8Array> {
  try {
    return await argon2id({
      password,
      salt,
      iterations: VIGIL_KDF_PARAMS.iterations,
      parallelism: VIGIL_KDF_PARAMS.parallelism,
      memorySize: VIGIL_KDF_PARAMS.memory_kib,
      hashLength: VIGIL_KDF_PARAMS.length,
      outputType: "binary",
    });
  } catch (cause) {
    throw new VigilCryptoUnavailableError(
      "Argon2id key derivation failed (unsupported browser or insufficient memory)",
      { cause },
    );
  }
}
