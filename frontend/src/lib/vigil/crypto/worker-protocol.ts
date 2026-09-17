// Message shapes shared between the main thread (worker-client.ts) and
// the dedicated Worker (worker.ts). Binary payloads travel as
// Transferable ArrayBuffers, never copied, so the plaintext/password
// bytes exist in exactly one place at a time and are dropped by the
// sender the instant they're handed off.
import type { VigilManifest } from "./manifest";

export interface VigilWorkerEncryptRequest {
  type: "encrypt";
  requestId: string;
  vaultId: string;
  objectId: string;
  plaintext: ArrayBuffer;
  /** null = explicit no-password choice. */
  password: ArrayBuffer | null;
}

export interface VigilWorkerEncryptSuccess {
  type: "encrypt-success";
  requestId: string;
  manifest: VigilManifest;
  inner: ArrayBuffer;
  ciphertext: ArrayBuffer;
}

export interface VigilWorkerEncryptFailure {
  type: "encrypt-error";
  requestId: string;
  message: string;
}

export type VigilWorkerResponse = VigilWorkerEncryptSuccess | VigilWorkerEncryptFailure;
