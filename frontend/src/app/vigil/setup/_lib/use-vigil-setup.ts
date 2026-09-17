"use client";

// State machine backing /vigil/setup (issue #455). Owns the whole form
// plus the multi-step submit pipeline (configuration -> object init ->
// browser encryption -> upload) and its retry-safety cache.
//
// Retry-safety contract (P2.2-A04/=A05, Appendix B "Browser retry resends
// same bytes; changed file/password gets fresh ID/DEK/nonces"):
//   - `objectCacheRef` is keyed by a fingerprint of (file identity +
//     password choice + password value). A plain retry after a failed
//     upload recomputes the SAME fingerprint, so the cached
//     {objectId, manifest, inner, ciphertext} is resent unchanged — no
//     new randomness, no new Argon2id run, no new object_id.
//   - Changing the file OR the password changes the fingerprint, which
//     forces a fresh `POST /vigil/objects/init` (new object_id) and a
//     fresh Worker encryption pass (fresh DEK/nonces/salt). This also
//     matches a real backend constraint, not just the written contract:
//     once an object reaches `status=ready`, re-uploading different
//     ciphertext under the SAME object_id is a 409 conflict server-side
//     (services/vigil/objects.py's `upload_object`) — a password change
//     genuinely cannot reuse the prior object_id once uploaded.
//   - `configCacheRef` is a separate, coarser cache keyed by
//     (recipients, message, interval, grace) so retrying a failed upload
//     never creates a redundant new `vigil_configurations` row just
//     because the file/password fingerprint also happened to change.
import { useCallback, useEffect, useRef, useState } from "react";

import {
  createVigilConfiguration,
  getVigilVaultStatus,
  initVigilObject,
  uploadVigilObject,
  VigilApiError,
  VigilRevisionConflictError,
  type VigilVaultStatus,
} from "@/lib/vigil/api";
import { encodeBase64Url } from "@/lib/vigil/crypto/base64url";
import type { VigilManifest } from "@/lib/vigil/crypto/manifest";
import { VigilCryptoWorkerClient } from "@/lib/vigil/crypto/worker-client";
import {
  VIGIL_GRACE_HOURS_DEFAULT,
  VIGIL_INTERVAL_DAYS_DEFAULT,
  validateSetupForm,
  type SetupFormErrorCode,
  type SetupRecipientForm,
} from "./validation";

export type VigilSetupPhase = "form" | "submitting" | "ready" | "error";
export type VigilSetupStage = "configuring" | "preparing" | "encrypting" | "uploading" | null;

interface VigilCryptoClientLike {
  encryptFile(input: {
    vaultId: string;
    objectId: string;
    plaintext: Uint8Array;
    password: Uint8Array | null;
  }): Promise<{ manifest: VigilManifest; inner: Uint8Array; ciphertext: Uint8Array }>;
  terminate(): void;
}

interface EncryptedObject {
  manifest: VigilManifest;
  inner: Uint8Array;
  ciphertext: Uint8Array;
}

interface ObjectCacheEntry {
  fingerprint: string;
  objectId: string;
  encrypted: EncryptedObject;
}

interface ConfigCacheEntry {
  fingerprint: string;
  configId: string;
}

export interface UseVigilSetupOptions {
  /** Injectable for tests; defaults to a real Worker-backed client. */
  cryptoClient?: VigilCryptoClientLike;
}

function fileIdentity(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

function errorMessageFor(err: unknown): { message: string; code: string } {
  if (err instanceof VigilRevisionConflictError) {
    return { message: "revisionConflict", code: "revisionConflict" };
  }
  if (err instanceof VigilApiError) {
    return { message: typeof err.detail === "string" ? err.detail : `apiError:${err.status}`, code: "apiError" };
  }
  if (err instanceof DOMException && err.name === "AbortError") {
    return { message: "cancelled", code: "cancelled" };
  }
  if (err instanceof Error) {
    return { message: err.message, code: "unknown" };
  }
  return { message: "unknown error", code: "unknown" };
}

export function useVigilSetup(options: UseVigilSetupOptions = {}) {
  const [vaultStatus, setVaultStatus] = useState<VigilVaultStatus | null>(null);
  const [vaultStatusError, setVaultStatusError] = useState(false);

  const [file, setFile] = useState<File | null>(null);
  const [hasPassword, setHasPassword] = useState<boolean | null>(null);
  const [password, setPassword] = useState("");
  const [passwordConfirm, setPasswordConfirm] = useState("");
  const [recipients, setRecipients] = useState<SetupRecipientForm[]>([{ email: "", email_confirm: "" }]);
  const [message, setMessage] = useState("");
  const [intervalDays, setIntervalDays] = useState<number>(VIGIL_INTERVAL_DAYS_DEFAULT);
  const [graceHours, setGraceHours] = useState<number>(VIGIL_GRACE_HOURS_DEFAULT);

  const [phase, setPhase] = useState<VigilSetupPhase>("form");
  const [stage, setStage] = useState<VigilSetupStage>(null);
  const [progress, setProgress] = useState(0);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [validationErrors, setValidationErrors] = useState<SetupFormErrorCode[]>([]);

  const revisionRef = useRef(0);
  const vaultIdRef = useRef<string | null>(null);
  const configCacheRef = useRef<ConfigCacheEntry | null>(null);
  const objectCacheRef = useRef<ObjectCacheEntry | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const cryptoClientRef = useRef<VigilCryptoClientLike | null>(options.cryptoClient ?? null);

  useEffect(() => {
    let cancelled = false;
    getVigilVaultStatus()
      .then((status) => {
        if (cancelled) return;
        setVaultStatus(status);
        revisionRef.current = status.revision;
        vaultIdRef.current = status.vault_id;
      })
      .catch(() => {
        if (!cancelled) setVaultStatusError(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(
    () => () => {
      cryptoClientRef.current?.terminate();
    },
    [],
  );

  function ensureCryptoClient(): VigilCryptoClientLike {
    if (!cryptoClientRef.current) {
      cryptoClientRef.current = new VigilCryptoWorkerClient();
    }
    return cryptoClientRef.current;
  }

  const cancel = useCallback(() => {
    abortControllerRef.current?.abort();
    // A Worker encryption call has no abort signal of its own — the only
    // way to actually stop it mid-computation is to discard the Worker.
    // `ensureCryptoClient` lazily creates a fresh one on the next submit.
    cryptoClientRef.current?.terminate();
    cryptoClientRef.current = null;
    setPhase("form");
    setStage(null);
    setProgress(0);
    setPassword("");
    setPasswordConfirm("");
  }, []);

  const submit = useCallback(async () => {
    const errors = validateSetupForm({
      fileSize: file ? file.size : null,
      hasPassword,
      password,
      passwordConfirm,
      recipients,
      message,
      intervalDays,
      graceHours,
    });
    setValidationErrors(errors);
    if (errors.length > 0 || !file || hasPassword === null) {
      return;
    }

    setPhase("submitting");
    setErrorMessage(null);
    setErrorCode(null);
    setProgress(0);
    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      const configFingerprint = JSON.stringify({ recipients, message, intervalDays, graceHours });
      if (configCacheRef.current?.fingerprint !== configFingerprint) {
        setStage("configuring");
        const result = await createVigilConfiguration({
          expected_revision: revisionRef.current,
          interval_days: intervalDays,
          grace_hours: graceHours,
          recipients,
          message,
        });
        revisionRef.current = result.revision;
        vaultIdRef.current = result.vault_id;
        configCacheRef.current = { fingerprint: configFingerprint, configId: result.config_id };
        // A new configuration means any previously-encrypted object no
        // longer targets a config_id this attempt will actually use.
        objectCacheRef.current = null;
      }
      const configId = configCacheRef.current.configId;
      const vaultId = vaultIdRef.current;
      if (!vaultId) {
        throw new Error("no vault_id available after configuration");
      }

      const passwordBytes = hasPassword ? new TextEncoder().encode(password) : null;
      const objectFingerprint = `${fileIdentity(file)}:${hasPassword}:${hasPassword ? password : ""}`;

      let objectId: string;
      let encrypted: EncryptedObject;

      if (objectCacheRef.current?.fingerprint === objectFingerprint) {
        objectId = objectCacheRef.current.objectId;
        encrypted = objectCacheRef.current.encrypted;
      } else {
        setStage("preparing");
        const requestId = crypto.randomUUID();
        const plaintext = new Uint8Array(await file.arrayBuffer());
        const initResult = await initVigilObject({
          expected_revision: revisionRef.current,
          config_id: configId,
          request_id: requestId,
          filename: file.name,
          plaintext_size: plaintext.length,
        });
        revisionRef.current = initResult.revision;
        objectId = initResult.object_id;

        setStage("encrypting");
        const cryptoClient = ensureCryptoClient();
        encrypted = await cryptoClient.encryptFile({
          vaultId,
          objectId,
          plaintext,
          password: passwordBytes,
        });

        objectCacheRef.current = { fingerprint: objectFingerprint, objectId, encrypted };
      }

      setStage("uploading");
      const uploadResult = await uploadVigilObject(
        {
          expected_revision: revisionRef.current,
          config_id: configId,
          object_id: objectId,
          manifest: encrypted.manifest,
          inner: encodeBase64Url(encrypted.inner),
          ciphertext: encrypted.ciphertext,
        },
        { signal: controller.signal, onProgress: setProgress },
      );
      revisionRef.current = uploadResult.revision;

      setPhase("ready");
      setStage(null);
      setPassword("");
      setPasswordConfirm("");
    } catch (err) {
      const { message: msg, code } = errorMessageFor(err);
      if (err instanceof VigilRevisionConflictError) {
        revisionRef.current = err.currentRevision;
      }
      if (code === "cancelled") {
        setPhase("form");
        setStage(null);
        return;
      }
      setErrorMessage(msg);
      setErrorCode(code);
      setPhase("error");
      setStage(null);
    }
  }, [file, hasPassword, password, passwordConfirm, recipients, message, intervalDays, graceHours]);

  return {
    vaultStatus,
    vaultStatusError,

    file,
    setFile,
    hasPassword,
    setHasPassword,
    password,
    setPassword,
    passwordConfirm,
    setPasswordConfirm,
    recipients,
    setRecipients,
    message,
    setMessage,
    intervalDays,
    setIntervalDays,
    graceHours,
    setGraceHours,

    phase,
    stage,
    progress,
    errorMessage,
    errorCode,
    validationErrors,

    submit,
    cancel,
  };
}
