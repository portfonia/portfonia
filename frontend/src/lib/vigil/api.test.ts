import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createVigilConfiguration,
  getVigilVaultStatus,
  initVigilObject,
  uploadVigilObject,
  VigilApiError,
  VigilRevisionConflictError,
} from "./api";
import type { VigilManifest } from "./crypto/manifest";

const originalFetch = global.fetch;

describe("getVigilVaultStatus", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("calls the same-origin /api/vigil/vault rewrite path with no-store caching", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ vault_id: null, phase: "DISARMED", revision: 0 }), { status: 200 }));
    global.fetch = fetchMock;

    await getVigilVaultStatus();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/vigil/vault");
    expect(init.cache).toBe("no-store");
  });

  it("resolves the parsed body on 200, matching the current #504 null-vault shape", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ vault_id: null, phase: "DISARMED", revision: 0 }), { status: 200 }),
    );

    const result = await getVigilVaultStatus();

    expect(result).toEqual({ vault_id: null, phase: "DISARMED", revision: 0 });
  });

  // The nav-visibility check must fail closed on every non-2xx status,
  // including 403 (not the owner) and 503 (feature off) — it must never
  // itself call the shared logout() Server Action the way lib/api.ts's
  // throwOnHttpError does, since a decorative "should the Vigil menu entry
  // show" check has no business forcing a session-wide logout redirect on a
  // plain 401/403.
  it.each([401, 403, 503])("rejects without redirecting on a %d response", async (status) => {
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status }));

    await expect(getVigilVaultStatus()).rejects.toThrow(String(status));
  });
});

describe("createVigilConfiguration / initVigilObject", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("createVigilConfiguration posts JSON to /api/vigil/configurations and resolves the body", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ vault_id: "v1", config_id: "c1", revision: 1 }), { status: 201 }),
      );
    global.fetch = fetchMock;

    const result = await createVigilConfiguration({
      expected_revision: 0,
      recipients: [{ email: "a@example.com", email_confirm: "a@example.com" }],
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/vigil/configurations");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      expected_revision: 0,
      recipients: [{ email: "a@example.com", email_confirm: "a@example.com" }],
    });
    expect(result).toEqual({ vault_id: "v1", config_id: "c1", revision: 1 });
  });

  it("createVigilConfiguration throws VigilRevisionConflictError on a 409 revision_conflict body", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: { error: "revision_conflict", current_revision: 5 } }), {
        status: 409,
      }),
    );

    const err = await createVigilConfiguration({
      expected_revision: 0,
      recipients: [{ email: "a@example.com", email_confirm: "a@example.com" }],
    }).catch((e: unknown) => e);

    expect(err).toBeInstanceOf(VigilRevisionConflictError);
    expect((err as VigilRevisionConflictError).currentRevision).toBe(5);
  });

  it("createVigilConfiguration throws a generic VigilApiError on 422", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ detail: "recipients must have between 1 and 3 entries" }), { status: 422 }));

    const err = await createVigilConfiguration({ expected_revision: 0, recipients: [] }).catch((e: unknown) => e);

    expect(err).toBeInstanceOf(VigilApiError);
    expect((err as VigilApiError).status).toBe(422);
    expect((err as VigilApiError).detail).toBe("recipients must have between 1 and 3 entries");
  });

  it("initVigilObject posts JSON to /api/vigil/objects/init and resolves the body", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ object_id: "o1", revision: 2 }), { status: 201 }));
    global.fetch = fetchMock;

    const result = await initVigilObject({
      expected_revision: 1,
      config_id: "c1",
      request_id: "r1",
      filename: "will.pdf",
      plaintext_size: 1024,
    });

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/vigil/objects/init");
    expect(result).toEqual({ object_id: "o1", revision: 2 });
  });

  it("initVigilObject throws VigilApiError with a 404 for an object_id/config_id owned by another vault", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ detail: "config_id does not belong to this vault" }), { status: 404 }));

    const err = await initVigilObject({
      expected_revision: 1,
      config_id: "not-mine",
      request_id: "r1",
      filename: "x",
      plaintext_size: 1,
    }).catch((e: unknown) => e);

    expect(err).toBeInstanceOf(VigilApiError);
    expect((err as VigilApiError).status).toBe(404);
  });
});

// jsdom's XMLHttpRequest doesn't perform real network I/O against a
// relative URL in this test environment, and uploadVigilObject is built
// on XHR specifically for `upload.onprogress` (fetch cannot report
// upload progress) — a fake XHR stands in, the same way FakeWorker stands
// in for the real crypto Worker in worker-client.test.ts.
class FakeXMLHttpRequest {
  static instances: FakeXMLHttpRequest[] = [];

  method = "";
  url = "";
  status = 0;
  response: unknown = null;
  responseType = "";
  sentBody: FormData | null = null;
  upload = { onprogress: null as ((event: ProgressEvent) => void) | null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;

  constructor() {
    FakeXMLHttpRequest.instances.push(this);
  }

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  send(body: FormData) {
    this.sentBody = body;
  }

  abort() {
    this.onabort?.();
  }

  respond(status: number, response: unknown) {
    this.status = status;
    this.response = response;
    this.onload?.();
  }

  progress(loaded: number, total: number) {
    this.upload.onprogress?.({ lengthComputable: true, loaded, total } as ProgressEvent);
  }
}

describe("uploadVigilObject", () => {
  const originalXhr = global.XMLHttpRequest;

  afterEach(() => {
    global.XMLHttpRequest = originalXhr;
    FakeXMLHttpRequest.instances = [];
  });

  const manifest: VigilManifest = {
    version: 1,
    algorithm: "AES-256-GCM",
    vault_id: "v1",
    object_id: "o1",
    has_password: false,
    file_nonce: "AAAAAAAAAAAAAAAA",
    salt: null,
    kdf: null,
    inner_nonce: null,
  };

  it("POSTs multipart form fields matching the backend's exact field names", async () => {
    global.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest;

    const promise = uploadVigilObject({
      expected_revision: 3,
      config_id: "c1",
      object_id: "o1",
      manifest,
      inner: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      ciphertext: new Uint8Array([1, 2, 3, 4]),
    });

    const xhr = FakeXMLHttpRequest.instances[0];
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("/api/vigil/objects/upload");
    const form = xhr.sentBody as FormData;
    expect(form.get("expected_revision")).toBe("3");
    expect(form.get("config_id")).toBe("c1");
    expect(form.get("object_id")).toBe("o1");
    expect(JSON.parse(form.get("manifest") as string)).toEqual(manifest);
    expect(form.get("inner")).toBe("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA");
    expect(form.get("file")).toBeInstanceOf(Blob);

    xhr.respond(201, { object_id: "o1", status: "ready", revision: 4 });
    await expect(promise).resolves.toEqual({ object_id: "o1", status: "ready", revision: 4 });
  });

  it("reports upload progress via onProgress from real XHR upload byte counts", async () => {
    global.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest;
    const progressValues: number[] = [];

    const promise = uploadVigilObject(
      {
        expected_revision: 0,
        config_id: "c1",
        object_id: "o1",
        manifest,
        inner: "AAAA",
        ciphertext: new Uint8Array(10),
      },
      { onProgress: (fraction) => progressValues.push(fraction) },
    );

    const xhr = FakeXMLHttpRequest.instances[0];
    xhr.progress(5, 10);
    xhr.progress(10, 10);
    xhr.respond(201, { object_id: "o1", status: "ready", revision: 1 });
    await promise;

    expect(progressValues).toEqual([0.5, 1]);
  });

  it("rejects with VigilRevisionConflictError when the XHR response is a 409 revision_conflict body", async () => {
    global.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest;

    const promise = uploadVigilObject({
      expected_revision: 0,
      config_id: "c1",
      object_id: "o1",
      manifest,
      inner: "AAAA",
      ciphertext: new Uint8Array(1),
    });

    const xhr = FakeXMLHttpRequest.instances[0];
    xhr.respond(409, { detail: { error: "revision_conflict", current_revision: 7 } });

    const err = await promise.catch((e: unknown) => e);
    expect(err).toBeInstanceOf(VigilRevisionConflictError);
    expect((err as VigilRevisionConflictError).currentRevision).toBe(7);
  });

  it("rejects with AbortError when aborted before completion, never resolving with a fallback", async () => {
    global.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest;
    const controller = new AbortController();

    const promise = uploadVigilObject(
      {
        expected_revision: 0,
        config_id: "c1",
        object_id: "o1",
        manifest,
        inner: "AAAA",
        ciphertext: new Uint8Array(1),
      },
      { signal: controller.signal },
    );
    controller.abort();

    await expect(promise).rejects.toThrow("upload aborted");
  });

  it("rejects immediately if the signal is already aborted, without touching XMLHttpRequest", async () => {
    global.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest;
    const controller = new AbortController();
    controller.abort();

    await expect(
      uploadVigilObject(
        {
          expected_revision: 0,
          config_id: "c1",
          object_id: "o1",
          manifest,
          inner: "AAAA",
          ciphertext: new Uint8Array(1),
        },
        { signal: controller.signal },
      ),
    ).rejects.toThrow("upload aborted");
    expect(FakeXMLHttpRequest.instances).toHaveLength(0);
  });
});
