import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const {
  getVigilVaultStatus,
  createVigilConfiguration,
  initVigilObject,
  uploadVigilObject,
  VigilRevisionConflictErrorCtor,
  VigilApiErrorCtor,
} = vi.hoisted(() => {
  class VigilRevisionConflictErrorCtor extends Error {
    currentRevision: number;
    constructor(currentRevision: number) {
      super("vigil vault revision conflict");
      this.name = "VigilRevisionConflictError";
      this.currentRevision = currentRevision;
    }
  }
  class VigilApiErrorCtor extends Error {
    status: number;
    detail: unknown;
    constructor(status: number, detail: unknown) {
      super(`vigil request failed: ${status}`);
      this.name = "VigilApiError";
      this.status = status;
      this.detail = detail;
    }
  }
  return {
    getVigilVaultStatus: vi.fn(),
    createVigilConfiguration: vi.fn(),
    initVigilObject: vi.fn(),
    uploadVigilObject: vi.fn(),
    VigilRevisionConflictErrorCtor,
    VigilApiErrorCtor,
  };
});

vi.mock("@/lib/vigil/api", () => ({
  getVigilVaultStatus,
  createVigilConfiguration,
  initVigilObject,
  uploadVigilObject,
  VigilRevisionConflictError: VigilRevisionConflictErrorCtor,
  VigilApiError: VigilApiErrorCtor,
}));

import { useVigilSetup } from "./use-vigil-setup";

function makeFile(name: string, bytes: number, content = "x"): File {
  const buf = new Uint8Array(bytes).fill(content.charCodeAt(0));
  return new File([buf], name, { type: "application/octet-stream" });
}

function fakeCryptoClient() {
  return {
    encryptFile: vi.fn().mockImplementation(
      async ({ vaultId, objectId }: { vaultId: string; objectId: string }) => ({
        manifest: {
          version: 1,
          algorithm: "AES-256-GCM",
          vault_id: vaultId,
          object_id: objectId,
          has_password: false,
          file_nonce: "AAAAAAAAAAAAAAAA",
          salt: null,
          kdf: null,
          inner_nonce: null,
        },
        inner: new Uint8Array(32),
        ciphertext: new Uint8Array(16),
      }),
    ),
    terminate: vi.fn(),
  };
}

const DEFAULT_RECIPIENTS = [{ email: "a@example.com", email_confirm: "a@example.com" }];

afterEach(() => {
  vi.resetAllMocks();
});

describe("useVigilSetup", () => {
  it("loads the current vault revision/vault_id on mount", async () => {
    getVigilVaultStatus.mockResolvedValue({ vault_id: "v1", phase: "ARMED", revision: 5 });
    const { result } = renderHook(() => useVigilSetup({ cryptoClient: fakeCryptoClient() }));

    await waitFor(() => expect(result.current.vaultStatus).not.toBeNull());
    expect(result.current.vaultStatus?.revision).toBe(5);
    expect(result.current.vaultStatus?.vault_id).toBe("v1");
  });

  it("runs the full pipeline in order on first submit and reaches phase 'ready'", async () => {
    getVigilVaultStatus.mockResolvedValue({ vault_id: null, phase: "DISARMED", revision: 0 });
    createVigilConfiguration.mockResolvedValue({ vault_id: "v1", config_id: "c1", revision: 1 });
    initVigilObject.mockResolvedValue({ object_id: "o1", revision: 2 });
    uploadVigilObject.mockResolvedValue({ object_id: "o1", status: "ready", revision: 3 });
    const crypto = fakeCryptoClient();

    const { result } = renderHook(() => useVigilSetup({ cryptoClient: crypto }));
    await waitFor(() => expect(result.current.vaultStatus).not.toBeNull());

    act(() => {
      result.current.setFile(makeFile("will.pdf", 1024));
      result.current.setHasPassword(false);
      result.current.setRecipients(DEFAULT_RECIPIENTS);
    });

    await act(async () => {
      await result.current.submit();
    });

    expect(createVigilConfiguration).toHaveBeenCalledWith(
      expect.objectContaining({ expected_revision: 0, recipients: DEFAULT_RECIPIENTS }),
    );
    expect(initVigilObject).toHaveBeenCalledWith(
      expect.objectContaining({ expected_revision: 1, config_id: "c1", filename: "will.pdf", plaintext_size: 1024 }),
    );
    expect(crypto.encryptFile).toHaveBeenCalledWith(
      expect.objectContaining({ vaultId: "v1", objectId: "o1", password: null }),
    );
    expect(uploadVigilObject).toHaveBeenCalledWith(
      expect.objectContaining({ expected_revision: 2, config_id: "c1", object_id: "o1" }),
      expect.anything(),
    );
    expect(result.current.phase).toBe("ready");
  });

  it("a plain retry after an upload failure resends byte-identical crypto material with no re-encryption (P2.2-A04)", async () => {
    getVigilVaultStatus.mockResolvedValue({ vault_id: null, phase: "DISARMED", revision: 0 });
    createVigilConfiguration.mockResolvedValue({ vault_id: "v1", config_id: "c1", revision: 1 });
    initVigilObject.mockResolvedValue({ object_id: "o1", revision: 2 });
    uploadVigilObject.mockRejectedValueOnce(new Error("network error"));
    uploadVigilObject.mockResolvedValueOnce({ object_id: "o1", status: "ready", revision: 3 });
    const crypto = fakeCryptoClient();

    const { result } = renderHook(() => useVigilSetup({ cryptoClient: crypto }));
    await waitFor(() => expect(result.current.vaultStatus).not.toBeNull());

    act(() => {
      result.current.setFile(makeFile("will.pdf", 1024));
      result.current.setHasPassword(false);
      result.current.setRecipients(DEFAULT_RECIPIENTS);
    });

    await act(async () => {
      await result.current.submit();
    });
    expect(result.current.phase).toBe("error");

    await act(async () => {
      await result.current.submit();
    });

    expect(result.current.phase).toBe("ready");
    // Configuration and object-init are NOT repeated on a plain retry.
    expect(createVigilConfiguration).toHaveBeenCalledTimes(1);
    expect(initVigilObject).toHaveBeenCalledTimes(1);
    // Encryption ran exactly once — the retry resent the cached bytes.
    expect(crypto.encryptFile).toHaveBeenCalledTimes(1);
    expect(uploadVigilObject).toHaveBeenCalledTimes(2);
    const firstCallArgs = uploadVigilObject.mock.calls[0][0];
    const secondCallArgs = uploadVigilObject.mock.calls[1][0];
    expect(secondCallArgs.manifest).toEqual(firstCallArgs.manifest);
    expect(secondCallArgs.inner).toEqual(firstCallArgs.inner);
    expect(secondCallArgs.ciphertext).toEqual(firstCallArgs.ciphertext);
    expect(secondCallArgs.object_id).toBe(firstCallArgs.object_id);
  });

  it("changing the password after a failed attempt allocates a fresh object ID and re-encrypts (P2.2-A04)", async () => {
    getVigilVaultStatus.mockResolvedValue({ vault_id: null, phase: "DISARMED", revision: 0 });
    createVigilConfiguration.mockResolvedValue({ vault_id: "v1", config_id: "c1", revision: 1 });
    initVigilObject.mockResolvedValueOnce({ object_id: "o1", revision: 2 });
    initVigilObject.mockResolvedValueOnce({ object_id: "o2", revision: 4 });
    uploadVigilObject.mockRejectedValueOnce(new Error("network error"));
    uploadVigilObject.mockResolvedValueOnce({ object_id: "o2", status: "ready", revision: 5 });
    const crypto = fakeCryptoClient();

    const { result } = renderHook(() => useVigilSetup({ cryptoClient: crypto }));
    await waitFor(() => expect(result.current.vaultStatus).not.toBeNull());

    const file = makeFile("will.pdf", 1024);
    act(() => {
      result.current.setFile(file);
      result.current.setHasPassword(true);
      result.current.setPassword("first-password");
      result.current.setPasswordConfirm("first-password");
      result.current.setRecipients(DEFAULT_RECIPIENTS);
    });

    await act(async () => {
      await result.current.submit();
    });
    expect(result.current.phase).toBe("error");

    act(() => {
      result.current.setPassword("second-password");
      result.current.setPasswordConfirm("second-password");
    });

    await act(async () => {
      await result.current.submit();
    });

    expect(result.current.phase).toBe("ready");
    expect(initVigilObject).toHaveBeenCalledTimes(2);
    expect(crypto.encryptFile).toHaveBeenCalledTimes(2);
    const secondUploadArgs = uploadVigilObject.mock.calls[1][0];
    expect(secondUploadArgs.object_id).toBe("o2");
  });

  it("changing the file after a failed attempt allocates a fresh object ID and re-encrypts (P2.2-A04)", async () => {
    getVigilVaultStatus.mockResolvedValue({ vault_id: null, phase: "DISARMED", revision: 0 });
    createVigilConfiguration.mockResolvedValue({ vault_id: "v1", config_id: "c1", revision: 1 });
    initVigilObject.mockResolvedValueOnce({ object_id: "o1", revision: 2 });
    initVigilObject.mockResolvedValueOnce({ object_id: "o2", revision: 4 });
    uploadVigilObject.mockRejectedValueOnce(new Error("network error"));
    uploadVigilObject.mockResolvedValueOnce({ object_id: "o2", status: "ready", revision: 5 });
    const crypto = fakeCryptoClient();

    const { result } = renderHook(() => useVigilSetup({ cryptoClient: crypto }));
    await waitFor(() => expect(result.current.vaultStatus).not.toBeNull());

    act(() => {
      result.current.setFile(makeFile("will.pdf", 1024));
      result.current.setHasPassword(false);
      result.current.setRecipients(DEFAULT_RECIPIENTS);
    });

    await act(async () => {
      await result.current.submit();
    });
    expect(result.current.phase).toBe("error");

    act(() => {
      // Same name/hasPassword, but a different underlying File — a new
      // selection through the file picker, even of a file that happens to
      // share a name, must not be mistaken for "the same file" (fileIdentity
      // also folds in size/lastModified, exercised here via a larger file).
      result.current.setFile(makeFile("will.pdf", 2048));
    });

    await act(async () => {
      await result.current.submit();
    });

    expect(result.current.phase).toBe("ready");
    // Configuration is unchanged (recipients/interval/grace/message didn't
    // change), but the object-init + encrypt steps must re-run for the new
    // file, unlike the plain-retry case above.
    expect(createVigilConfiguration).toHaveBeenCalledTimes(1);
    expect(initVigilObject).toHaveBeenCalledTimes(2);
    expect(initVigilObject).toHaveBeenLastCalledWith(
      expect.objectContaining({ filename: "will.pdf", plaintext_size: 2048 }),
    );
    expect(crypto.encryptFile).toHaveBeenCalledTimes(2);
    const secondUploadArgs = uploadVigilObject.mock.calls[1][0];
    expect(secondUploadArgs.object_id).toBe("o2");
  });

  it("a crypto worker failure never calls uploadVigilObject (P2.2-A02: no fallback upload)", async () => {
    getVigilVaultStatus.mockResolvedValue({ vault_id: null, phase: "DISARMED", revision: 0 });
    createVigilConfiguration.mockResolvedValue({ vault_id: "v1", config_id: "c1", revision: 1 });
    initVigilObject.mockResolvedValue({ object_id: "o1", revision: 2 });
    const crypto = {
      encryptFile: vi.fn().mockRejectedValue(new Error("Argon2id key derivation failed")),
      terminate: vi.fn(),
    };

    const { result } = renderHook(() => useVigilSetup({ cryptoClient: crypto }));
    await waitFor(() => expect(result.current.vaultStatus).not.toBeNull());

    act(() => {
      result.current.setFile(makeFile("will.pdf", 1024));
      result.current.setHasPassword(false);
      result.current.setRecipients(DEFAULT_RECIPIENTS);
    });

    await act(async () => {
      await result.current.submit();
    });

    expect(result.current.phase).toBe("error");
    expect(uploadVigilObject).not.toHaveBeenCalled();
  });

  it("a revision conflict updates the tracked revision and surfaces an error without crashing", async () => {
    getVigilVaultStatus.mockResolvedValue({ vault_id: null, phase: "DISARMED", revision: 0 });
    createVigilConfiguration.mockRejectedValue(new VigilRevisionConflictErrorCtor(7));

    const { result } = renderHook(() => useVigilSetup({ cryptoClient: fakeCryptoClient() }));
    await waitFor(() => expect(result.current.vaultStatus).not.toBeNull());

    act(() => {
      result.current.setFile(makeFile("will.pdf", 1024));
      result.current.setHasPassword(false);
      result.current.setRecipients(DEFAULT_RECIPIENTS);
    });

    await act(async () => {
      await result.current.submit();
    });

    expect(result.current.phase).toBe("error");
    expect(result.current.errorMessage).not.toBeNull();

    createVigilConfiguration.mockResolvedValueOnce({ vault_id: "v1", config_id: "c1", revision: 8 });
    initVigilObject.mockResolvedValue({ object_id: "o1", revision: 9 });
    uploadVigilObject.mockResolvedValue({ object_id: "o1", status: "ready", revision: 10 });

    await act(async () => {
      await result.current.submit();
    });

    expect(createVigilConfiguration).toHaveBeenLastCalledWith(expect.objectContaining({ expected_revision: 7 }));
    expect(result.current.phase).toBe("ready");
  });

  it("does not submit when the form fails validation, and reports the errors", async () => {
    getVigilVaultStatus.mockResolvedValue({ vault_id: null, phase: "DISARMED", revision: 0 });
    const { result } = renderHook(() => useVigilSetup({ cryptoClient: fakeCryptoClient() }));
    await waitFor(() => expect(result.current.vaultStatus).not.toBeNull());

    await act(async () => {
      await result.current.submit();
    });

    expect(createVigilConfiguration).not.toHaveBeenCalled();
    expect(result.current.phase).toBe("form");
    expect(result.current.validationErrors.length).toBeGreaterThan(0);
  });
});
