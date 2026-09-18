"use client";

// Main-thread wrapper around the dedicated crypto Worker (issue #455).
// Every call always mints fresh DEK/nonces/salt (or, for a password
// request, re-runs Argon2id) — this class does NOT decide when to reuse
// previously-produced ciphertext. That policy belongs one layer up, in
// the setup-page hook: a plain retry after an upload failure must resend
// the exact bytes already produced by a prior `encryptFile` call, never
// call this again, per the Browser v1 contract ("Browser retry resends same bytes;
// changed file/password gets fresh ID/DEK/nonces").
import { VigilCryptoUnavailableError } from "./errors";
import type { VigilManifest } from "./manifest";
import type { VigilWorkerEncryptRequest, VigilWorkerResponse } from "./worker-protocol";

export interface VigilEncryptFileInput {
  vaultId: string;
  objectId: string;
  plaintext: Uint8Array;
  password: Uint8Array | null;
}

export interface VigilEncryptFileResult {
  manifest: VigilManifest;
  inner: Uint8Array;
  ciphertext: Uint8Array;
}

interface PendingEntry {
  resolve: (result: VigilEncryptFileResult) => void;
  reject: (err: Error) => void;
}

function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

export class VigilCryptoWorkerClient {
  private worker: Worker | null = null;
  private readonly pending = new Map<string, PendingEntry>();

  private ensureWorker(): Worker {
    if (this.worker) return this.worker;
    if (typeof Worker === "undefined") {
      throw new VigilCryptoUnavailableError("Web Workers are not available in this browser");
    }
    const worker = new Worker(new URL("./worker.ts", import.meta.url), { type: "module" });
    worker.onmessage = (event: MessageEvent<VigilWorkerResponse>) => this.handleMessage(event.data);
    worker.onerror = (event) => this.failAllPending(event.message || "crypto worker crashed");
    worker.onmessageerror = () => this.failAllPending("crypto worker sent an unreadable message");
    this.worker = worker;
    return worker;
  }

  private handleMessage(message: VigilWorkerResponse): void {
    const entry = this.pending.get(message.requestId);
    if (!entry) return;
    this.pending.delete(message.requestId);
    if (message.type === "encrypt-success") {
      entry.resolve({
        manifest: message.manifest,
        inner: new Uint8Array(message.inner),
        ciphertext: new Uint8Array(message.ciphertext),
      });
    } else {
      entry.reject(new VigilCryptoUnavailableError(message.message));
    }
  }

  // A crashed/unreachable worker fails every in-flight request — never
  // leaves one hanging to time out silently, and never spins up a
  // same-thread fallback encryption path.
  private failAllPending(message: string): void {
    for (const [id, entry] of this.pending) {
      entry.reject(new VigilCryptoUnavailableError(message));
      this.pending.delete(id);
    }
    this.worker = null;
  }

  async encryptFile(input: VigilEncryptFileInput): Promise<VigilEncryptFileResult> {
    const worker = this.ensureWorker();
    const requestId =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random()}`;

    const plaintextBuffer = toArrayBuffer(input.plaintext);
    const passwordBuffer = input.password ? toArrayBuffer(input.password) : null;

    return new Promise<VigilEncryptFileResult>((resolve, reject) => {
      this.pending.set(requestId, { resolve, reject });
      const request: VigilWorkerEncryptRequest = {
        type: "encrypt",
        requestId,
        vaultId: input.vaultId,
        objectId: input.objectId,
        plaintext: plaintextBuffer,
        password: passwordBuffer,
      };
      const transfer: Transferable[] = [plaintextBuffer];
      if (passwordBuffer) transfer.push(passwordBuffer);
      worker.postMessage(request, transfer);
    });
  }

  terminate(): void {
    this.worker?.terminate();
    this.worker = null;
    for (const [id, entry] of this.pending) {
      entry.reject(new VigilCryptoUnavailableError("crypto worker terminated"));
      this.pending.delete(id);
    }
  }
}
