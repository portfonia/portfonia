// Client-side typed access to the Vigil management API, browser side.
//
// Mirrors backend/app/schemas/vigil.py's VigilVaultStatus/VigilObjectSummary
// exactly as they exist today (#452/#504) — not the aspirational full shape
// from #450's Design section 5. `active`/`pending`/`recipients`/
// `delivery_status` are structurally present but always empty/None until
// #454+ adds the configurations/objects tables; do not add `inner`/`outer`/
// DEK fields here even speculatively — the backend never returns them and a
// client type that accepted them would be exactly the leak surface #453's
// scope explicitly forbids.
import type { VigilManifest } from "./crypto/manifest";

export interface VigilObjectSummary {
  id: string;
  filename: string;
  plaintext_size: number;
  status: string;
  has_password: boolean | null;
}

export interface VigilVaultStatus {
  vault_id: string | null;
  phase: string;
  revision: number;
  hold_reason?: string | null;
  next_check_at?: string | null;
  deadline_at?: string | null;
  last_scan_completed_at?: string | null;
  active?: VigilObjectSummary | null;
  pending?: VigilObjectSummary | null;
  recipients?: string[];
  delivery_status?: unknown[];
}

// Deliberately does NOT reuse lib/api.ts's throwOnHttpError: that helper
// calls the shared logout() Server Action on a 401, which is the right
// behavior for an actual page's data fetch but wrong for this endpoint's
// two current call sites (the SiteHeader nav-visibility check and the
// /vigil dashboard's own read) — see hooks/use-vigil-access.ts and
// app/vigil/page.tsx for how each one separately decides what a non-2xx
// response means (hidden nav entry vs. an "unavailable" dashboard state).
export async function getVigilVaultStatus(): Promise<VigilVaultStatus> {
  const res = await fetch("/api/vigil/vault", { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`vigil vault status request failed: ${res.status}`);
  }
  return res.json() as Promise<VigilVaultStatus>;
}

// --- issue #455 (Vigil R0 P2.2): setup-page client calls -------------

// A stale `expected_revision` (backend's optimistic-concurrency lock,
// #450 Design section 3) is a distinct, recoverable case from every other
// failure — the setup UI needs the server's authoritative current
// revision to refresh and let the owner retry, not just a generic error.
export class VigilRevisionConflictError extends Error {
  readonly currentRevision: number;

  constructor(currentRevision: number) {
    super("vigil vault revision conflict");
    this.name = "VigilRevisionConflictError";
    this.currentRevision = currentRevision;
  }
}

export class VigilApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(`vigil request failed: ${status}`);
    this.name = "VigilApiError";
    this.status = status;
    this.detail = detail;
  }
}

function isRevisionConflictDetail(detail: unknown): detail is { current_revision: number } {
  return (
    typeof detail === "object" &&
    detail !== null &&
    (detail as { error?: unknown }).error === "revision_conflict" &&
    typeof (detail as { current_revision?: unknown }).current_revision === "number"
  );
}

function errorFromStatusAndDetail(status: number, detail: unknown): Error {
  if (status === 409 && isRevisionConflictDetail(detail)) {
    return new VigilRevisionConflictError(detail.current_revision);
  }
  return new VigilApiError(status, detail);
}

async function parseJsonError(res: Response): Promise<Error> {
  let detail: unknown = null;
  try {
    const body: unknown = await res.json();
    detail = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : body;
  } catch {
    // No JSON body (e.g. a 502 from the upload proxy) — detail stays null.
  }
  return errorFromStatusAndDetail(res.status, detail);
}

export interface VigilRecipientInput {
  email: string;
  email_confirm: string;
}

export interface VigilConfigurationInput {
  expected_revision: number;
  interval_days?: number;
  grace_hours?: number;
  recipients: VigilRecipientInput[];
  message?: string;
}

export interface VigilConfigurationResult {
  vault_id: string;
  config_id: string;
  revision: number;
}

export async function createVigilConfiguration(
  input: VigilConfigurationInput,
): Promise<VigilConfigurationResult> {
  const res = await fetch("/api/vigil/configurations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await parseJsonError(res);
  return res.json() as Promise<VigilConfigurationResult>;
}

export interface VigilObjectInitInput {
  expected_revision: number;
  config_id: string;
  request_id: string;
  filename: string;
  plaintext_size: number;
}

export interface VigilObjectInitResult {
  object_id: string;
  revision: number;
}

export async function initVigilObject(input: VigilObjectInitInput): Promise<VigilObjectInitResult> {
  const res = await fetch("/api/vigil/objects/init", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await parseJsonError(res);
  return res.json() as Promise<VigilObjectInitResult>;
}

export interface VigilObjectUploadInput {
  expected_revision: number;
  config_id: string;
  object_id: string;
  manifest: VigilManifest;
  /** Unpadded-base64url-encoded already (see lib/vigil/crypto/base64url.ts). */
  inner: string;
  ciphertext: Uint8Array;
}

export interface VigilObjectUploadResult {
  object_id: string;
  status: string;
  revision: number;
}

export interface VigilObjectUploadOptions {
  signal?: AbortSignal;
  /** Fraction in [0, 1], driven by the underlying XHR's real upload byte
   * count — a `fetch`-based POST cannot report upload progress, and this
   * is the one Vigil request body large enough (up to ~10 MB) to need it. */
  onProgress?: (fraction: number) => void;
}

// This is the one Vigil client call that goes through
// app/api/vigil/objects/upload/route.ts (a real Route Handler, not the
// plain rewrite) — see that file's own comment for why multipart needs
// it. Uses XMLHttpRequest rather than fetch specifically for
// `upload.onprogress` and reliable `.abort()`.
export function uploadVigilObject(
  input: VigilObjectUploadInput,
  options: VigilObjectUploadOptions = {},
): Promise<VigilObjectUploadResult> {
  return new Promise((resolve, reject) => {
    if (options.signal?.aborted) {
      reject(new DOMException("upload aborted", "AbortError"));
      return;
    }

    const form = new FormData();
    form.append("expected_revision", String(input.expected_revision));
    form.append("config_id", input.config_id);
    form.append("object_id", input.object_id);
    form.append("manifest", JSON.stringify(input.manifest));
    form.append("inner", input.inner);
    form.append("file", new Blob([new Uint8Array(input.ciphertext)]), "ciphertext.bin");

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/vigil/objects/upload");
    xhr.responseType = "json";

    if (options.onProgress) {
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) options.onProgress?.(event.loaded / event.total);
      };
    }

    xhr.onabort = () => reject(new DOMException("upload aborted", "AbortError"));
    xhr.onerror = () => reject(new Error("network error during vigil object upload"));
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.response as VigilObjectUploadResult);
      } else {
        const body = xhr.response as unknown;
        const detail =
          body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : body;
        reject(errorFromStatusAndDetail(xhr.status, detail));
      }
    };

    options.signal?.addEventListener("abort", () => xhr.abort());
    xhr.send(form);
  });
}

export interface VigilDrillResult {
  drill_id: string;
  status: string;
  revision: number;
}

export async function createVigilDrill(input: {
  expected_revision: number;
  config_id: string;
  object_id: string;
}): Promise<VigilDrillResult> {
  const res = await fetch("/api/vigil/drills", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await parseJsonError(res);
  return res.json() as Promise<VigilDrillResult>;
}

export interface VigilArmResult {
  phase: string;
  revision: number;
  next_check_at: string;
}

export async function armVigilVault(input: {
  expected_revision: number;
  config_id: string;
  object_id: string;
}): Promise<VigilArmResult> {
  const res = await fetch("/api/vigil/arm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await parseJsonError(res);
  return res.json() as Promise<VigilArmResult>;
}

export interface VigilPublicStatus {
  available: boolean;
  nonce?: string | null;
  expires_at?: string | null;
}

export async function getVigilPublicStatus(token: string): Promise<VigilPublicStatus> {
  const res = await fetch(`/api/vigil/public/status?token=${encodeURIComponent(token)}`, {
    cache: "no-store",
  });
  if (!res.ok) throw new VigilApiError(res.status, await res.text());
  return res.json() as Promise<VigilPublicStatus>;
}

export async function mintVigilPublicNonce(
  token: string,
  action: "confirm" | "revoke" | "metadata" | "material" | "ciphertext",
): Promise<VigilPublicStatus> {
  const res = await fetch("/api/vigil/public/status", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, action }),
  });
  if (!res.ok) throw new VigilApiError(res.status, await res.text());
  return res.json() as Promise<VigilPublicStatus>;
}

export interface VigilPublicConfirmResult {
  result: "confirmed" | "already_resolved" | "revoked";
  next_check_at: string | null;
}

export async function confirmVigilPublic(input: {
  token: string;
  nonce: string;
  altcha: string;
}): Promise<VigilPublicConfirmResult> {
  const res = await fetch("/api/vigil/public/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new VigilApiError(res.status, await res.text());
  return res.json() as Promise<VigilPublicConfirmResult>;
}
