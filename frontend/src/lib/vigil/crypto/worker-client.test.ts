import { afterEach, describe, expect, it, vi } from "vitest";

import { VigilCryptoUnavailableError } from "./errors";
import { VigilCryptoWorkerClient } from "./worker-client";
import type { VigilWorkerEncryptRequest, VigilWorkerResponse } from "./worker-protocol";

// jsdom has no real Worker/module-worker execution, and this test isn't
// trying to re-verify crypto correctness (encrypt-file.test.ts /
// worker.test.ts already do that with real primitives) — it verifies the
// client's own plumbing: request/response correlation, Transferable
// handling, and "a Worker failure never silently produces a fallback
// upload" (P2.2-A02). A fake Worker stands in for the real one.
class FakeWorker {
  onmessage: ((event: MessageEvent<VigilWorkerResponse>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  onmessageerror: ((event: MessageEvent) => void) | null = null;
  posted: VigilWorkerEncryptRequest[] = [];
  terminated = false;

  constructor(private readonly respond: (req: VigilWorkerEncryptRequest) => VigilWorkerResponse | "error") {}

  postMessage(message: VigilWorkerEncryptRequest) {
    this.posted.push(message);
    const outcome = this.respond(message);
    queueMicrotask(() => {
      if (outcome === "error") {
        this.onerror?.(new ErrorEvent("error", { message: "worker crashed" }));
      } else {
        this.onmessage?.({ data: outcome } as MessageEvent<VigilWorkerResponse>);
      }
    });
  }

  terminate() {
    this.terminated = true;
  }
}

function successResponse(requestId: string): VigilWorkerResponse {
  return {
    type: "encrypt-success",
    requestId,
    manifest: {
      version: 1,
      algorithm: "AES-256-GCM",
      vault_id: "v",
      object_id: "o",
      has_password: false,
      file_nonce: "AAAAAAAAAAAAAAAA",
      salt: null,
      kdf: null,
      inner_nonce: null,
    },
    inner: new Uint8Array(32).buffer,
    ciphertext: new Uint8Array(16).buffer,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("VigilCryptoWorkerClient", () => {
  it("throws VigilCryptoUnavailableError when Worker is not supported, with no fallback", async () => {
    vi.stubGlobal("Worker", undefined);
    const client = new VigilCryptoWorkerClient();
    await expect(
      client.encryptFile({ vaultId: "v", objectId: "o", plaintext: new Uint8Array(1), password: null }),
    ).rejects.toBeInstanceOf(VigilCryptoUnavailableError);
  });

  it("resolves with the decoded manifest/inner/ciphertext on encrypt-success", async () => {
    vi.stubGlobal(
      "Worker",
      class {
        constructor() {
          return new FakeWorker((req) => successResponse(req.requestId)) as unknown as Worker;
        }
      },
    );
    const client = new VigilCryptoWorkerClient();
    const result = await client.encryptFile({
      vaultId: "v",
      objectId: "o",
      plaintext: new Uint8Array([1, 2, 3]),
      password: null,
    });
    expect(result.manifest.has_password).toBe(false);
    expect(result.inner).toBeInstanceOf(Uint8Array);
    expect(result.ciphertext).toBeInstanceOf(Uint8Array);
  });

  it("rejects the pending request and never resolves with a fallback when the worker errors", async () => {
    vi.stubGlobal(
      "Worker",
      class {
        constructor() {
          return new FakeWorker(() => "error") as unknown as Worker;
        }
      },
    );
    const client = new VigilCryptoWorkerClient();
    await expect(
      client.encryptFile({ vaultId: "v", objectId: "o", plaintext: new Uint8Array(1), password: null }),
    ).rejects.toBeInstanceOf(VigilCryptoUnavailableError);
  });

  it("rejects the pending request on an encrypt-error response", async () => {
    vi.stubGlobal(
      "Worker",
      class {
        constructor() {
          return new FakeWorker((req) => ({
            type: "encrypt-error",
            requestId: req.requestId,
            message: "password must be 1-1024 UTF-8 bytes",
          })) as unknown as Worker;
        }
      },
    );
    const client = new VigilCryptoWorkerClient();
    await expect(
      client.encryptFile({ vaultId: "v", objectId: "o", plaintext: new Uint8Array(1), password: new Uint8Array(0) }),
    ).rejects.toThrow(/password/);
  });

  it("correlates concurrent requests by requestId independently", async () => {
    vi.stubGlobal(
      "Worker",
      class {
        constructor() {
          return new FakeWorker((req) => successResponse(req.requestId)) as unknown as Worker;
        }
      },
    );
    const client = new VigilCryptoWorkerClient();
    const [a, b] = await Promise.all([
      client.encryptFile({ vaultId: "v", objectId: "o1", plaintext: new Uint8Array(1), password: null }),
      client.encryptFile({ vaultId: "v", objectId: "o2", plaintext: new Uint8Array(1), password: null }),
    ]);
    expect(a.manifest.has_password).toBe(false);
    expect(b.manifest.has_password).toBe(false);
  });

  it("terminate() rejects any still-pending request and discards the worker", async () => {
    let capturedWorker: FakeWorker | undefined;
    vi.stubGlobal(
      "Worker",
      class {
        constructor() {
          capturedWorker = new FakeWorker(() => "error");
          // Never auto-respond for this test — we terminate before any response.
          capturedWorker.postMessage = () => {};
          return capturedWorker as unknown as Worker;
        }
      },
    );
    const client = new VigilCryptoWorkerClient();
    const pending = client.encryptFile({ vaultId: "v", objectId: "o", plaintext: new Uint8Array(1), password: null });
    client.terminate();
    await expect(pending).rejects.toBeInstanceOf(VigilCryptoUnavailableError);
    expect(capturedWorker?.terminated).toBe(true);
  });
});
