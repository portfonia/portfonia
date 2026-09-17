// Server-side (SSR) access to GET /vigil/vault, same rationale as
// lib/server-api.ts: a Server Component cannot use the browser /api rewrite
// proxy, so it calls the backend directly via BACKEND_URL and derives its
// own Authorization header from the SSR session cookie.
//
// Unlike lib/server-api.ts's readers, a non-2xx response here is not always
// an error the page should fail to render for: 403 (caller isn't the
// configured Vigil owner) and 503 (VIGIL_MODE off, or not configured) are
// both routine states the dashboard renders as its own "unavailable" card.
// Only 401 (session actually expired) and anything else unexpected route
// through the existing logout()/error handling.
import { logout } from "@/lib/auth-actions";
import { currentAccessToken } from "@/lib/supabase/server";
import type { VigilVaultStatus } from "./api";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export type VigilVaultLoadResult =
  | { status: "ok"; vault: VigilVaultStatus }
  | { status: "unavailable" }
  | { status: "error" };

async function authHeaders(): Promise<HeadersInit> {
  const token = await currentAccessToken();
  return token ? { authorization: `Bearer ${token}` } : {};
}

export async function getVigilVaultStatusServer(): Promise<VigilVaultLoadResult> {
  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}/vigil/vault`, {
      cache: "no-store",
      headers: await authHeaders(),
    });
  } catch {
    return { status: "error" };
  }

  if (res.status === 401) {
    // Same reasoning as server-api.ts's throwOnHttpError: this can be the
    // server-side idle/absolute-session-lifetime check firing on the first
    // request of a reopened tab. logout() always redirect()s, which always
    // throws — the code below never actually returns in production.
    await logout("expired");
  }
  if (res.status === 403 || res.status === 503) {
    return { status: "unavailable" };
  }
  if (!res.ok) {
    return { status: "error" };
  }
  return { status: "ok", vault: (await res.json()) as VigilVaultStatus };
}
