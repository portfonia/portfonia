// The dedicated crypto Worker entry point (issue #455). Split into a pure
// `handleEncryptRequest` (unit-tested directly, no Worker context needed)
// and a thin `self.onmessage` wiring at the bottom (only active when
// actually running inside a Worker — guarded so importing this module in
// a plain Node/jsdom test environment doesn't throw on a missing `self`).
//
// Never falls back on failure: any thrown error becomes an
// `encrypt-error` response, and the caller (worker-client.ts) treats that
// as terminal for the request — it does not retry with a weaker KDF or
// skip encryption.
import { encryptVigilFile } from "./encrypt-file";
import type { VigilWorkerEncryptRequest, VigilWorkerResponse } from "./worker-protocol";

function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

export async function handleEncryptRequest(
  request: VigilWorkerEncryptRequest,
): Promise<VigilWorkerResponse> {
  try {
    const result = await encryptVigilFile({
      vaultId: request.vaultId,
      objectId: request.objectId,
      plaintext: new Uint8Array(request.plaintext),
      password: request.password ? new Uint8Array(request.password) : null,
    });
    return {
      type: "encrypt-success",
      requestId: request.requestId,
      manifest: result.manifest,
      inner: toArrayBuffer(result.inner),
      ciphertext: toArrayBuffer(result.ciphertext),
    };
  } catch (err) {
    return {
      type: "encrypt-error",
      requestId: request.requestId,
      message: err instanceof Error ? err.message : "unknown Vigil crypto worker error",
    };
  }
}

function transferablesFor(response: VigilWorkerResponse): Transferable[] {
  return response.type === "encrypt-success" ? [response.inner, response.ciphertext] : [];
}

// tsconfig's `lib` is `["dom", ...]`, not `webworker` (the two conflict —
// a project can't declare both), so the real Worker global scope isn't
// typed here. This minimal shape is all this file needs, cast once
// through `unknown` rather than reaching for `any`.
interface VigilWorkerGlobalScope {
  postMessage: (message: VigilWorkerResponse, options?: { transfer?: Transferable[] }) => void;
  onmessage: ((event: MessageEvent<VigilWorkerEncryptRequest>) => void) | null;
}

const workerScope = globalThis as unknown as Partial<VigilWorkerGlobalScope>;

// `postMessage` alone isn't a reliable signal — jsdom's simulated Window
// has one too. A real DedicatedWorkerGlobalScope has no `window` (that's
// what distinguishes it from every DOM test environment this file might
// incidentally be imported into).
const isRealWorkerScope =
  typeof workerScope.postMessage === "function" &&
  typeof (globalThis as { window?: unknown }).window === "undefined";

if (isRealWorkerScope) {
  workerScope.onmessage = (event) => {
    void handleEncryptRequest(event.data).then((response) => {
      workerScope.postMessage?.(response, { transfer: transferablesFor(response) });
    });
  };
}
