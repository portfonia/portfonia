// @vitest-environment node
import { describe, expect, it } from "vitest";

import { handleEncryptRequest } from "./worker";
import type { VigilWorkerEncryptRequest } from "./worker-protocol";

function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

describe("handleEncryptRequest (the Worker's pure message handler)", () => {
  it("returns an encrypt-success response with transferable ArrayBuffers", async () => {
    const request: VigilWorkerEncryptRequest = {
      type: "encrypt",
      requestId: "req-1",
      vaultId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      objectId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      plaintext: toArrayBuffer(new TextEncoder().encode("hello vigil")),
      password: null,
    };

    const response = await handleEncryptRequest(request);
    expect(response.type).toBe("encrypt-success");
    if (response.type !== "encrypt-success") throw new Error("unreachable");
    expect(response.requestId).toBe("req-1");
    expect(response.manifest.has_password).toBe(false);
    expect(response.inner).toBeInstanceOf(ArrayBuffer);
    expect(response.ciphertext).toBeInstanceOf(ArrayBuffer);
    expect(response.ciphertext.byteLength).toBe("hello vigil".length + 16);
  });

  it("returns an encrypt-success response for a password request", async () => {
    const request: VigilWorkerEncryptRequest = {
      type: "encrypt",
      requestId: "req-2",
      vaultId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      objectId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      plaintext: toArrayBuffer(new TextEncoder().encode("secret file")),
      password: toArrayBuffer(new TextEncoder().encode("s3cr3t")),
    };

    const response = await handleEncryptRequest(request);
    expect(response.type).toBe("encrypt-success");
    if (response.type !== "encrypt-success") throw new Error("unreachable");
    expect(response.manifest.has_password).toBe(true);
    expect(response.inner.byteLength).toBe(48);
  });

  it("returns an encrypt-error response (never throws) when the crypto pipeline fails", async () => {
    const request: VigilWorkerEncryptRequest = {
      type: "encrypt",
      requestId: "req-3",
      vaultId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      objectId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      plaintext: toArrayBuffer(new TextEncoder().encode("x")),
      // Empty password: outside the 1-1024 byte contract -> VigilCryptoInputError.
      password: toArrayBuffer(new Uint8Array(0)),
    };

    const response = await handleEncryptRequest(request);
    expect(response.type).toBe("encrypt-error");
    if (response.type !== "encrypt-error") throw new Error("unreachable");
    expect(response.requestId).toBe("req-3");
    expect(response.message).toMatch(/password/i);
  });
});
