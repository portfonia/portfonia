"use server";

import { headers } from "next/headers";

import { catalogs, DEFAULT_LOCALE, isLocale } from "@/locales";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export interface ForgotPasswordState {
  error: string | null;
  // undefined until the backend has actually answered — lets the form tell
  // "not submitted yet" apart from "submitted, account not found".
  accountFound?: boolean;
}

// See login/actions.ts's resolveLocale — same reasoning (no URL-based
// locale routing; the client form submits its current locale state).
function resolveLocale(formData: FormData) {
  const raw = String(formData.get("locale") ?? "");
  return isLocale(raw) ? raw : DEFAULT_LOCALE;
}

async function forgotPasswordBackendHeaders(): Promise<Record<string, string>> {
  // Same reasoning as signup/actions.ts's signupBackendHeaders: Browser ->
  // Caddy -> Next.js -> backend, so the ASGI peer is the frontend container
  // unless Caddy's client IP is forwarded (issue #190/#231 — the backend's
  // IP-bucket rate limit needs the real client IP, not the frontend's).
  const incoming = await headers();
  const out: Record<string, string> = { "Content-Type": "application/json" };
  const xff = incoming.get("x-forwarded-for");
  const realIp = incoming.get("x-real-ip");
  if (xff) {
    out["X-Forwarded-For"] = xff;
  }
  if (realIp) {
    out["X-Real-IP"] = realIp;
  }
  return out;
}

// Backend-mediated trigger (issue #231 — this is the "trigger" half of the
// architecture; consumption is client-direct to Supabase, same as login —
// see reset-password/reset-password-form.tsx, which has no Server Action at
// all). The Altcha payload comes from the widget's own hidden form field
// (AltchaWidget in _components/), not from anything this action computes.
export async function forgotPassword(
  _prevState: ForgotPasswordState | undefined,
  formData: FormData,
): Promise<ForgotPasswordState | undefined> {
  const email = String(formData.get("email") ?? "").trim();
  const altchaPayload = String(formData.get("altcha") ?? "");
  const auth = catalogs[resolveLocale(formData)].auth;

  if (!email) {
    return { error: auth.errorMissingCredentials };
  }
  if (!altchaPayload) {
    return { error: auth.forgotPasswordCaptchaRequired };
  }

  const res = await fetch(`${BACKEND_URL}/auth/forgot-password`, {
    method: "POST",
    headers: await forgotPasswordBackendHeaders(),
    body: JSON.stringify({ email, altcha: altchaPayload }),
  });

  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
    const detail = typeof body?.detail === "string" ? body.detail : null;
    if (res.status === 429) {
      return { error: detail ?? auth.tooManyAttempts };
    }
    if (res.status === 503) {
      return { error: detail ?? auth.temporarilyUnavailable };
    }
    if (res.status === 400) {
      return { error: detail ?? auth.forgotPasswordCaptchaRequired };
    }
    return { error: auth.forgotPasswordThrownError };
  }

  const body = (await res.json()) as { account_found: boolean };
  return { error: null, accountFound: body.account_found };
}
