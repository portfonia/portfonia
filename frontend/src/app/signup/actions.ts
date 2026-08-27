"use server";

import { headers } from "next/headers";
import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";
import { catalogs, DEFAULT_LOCALE, isLocale } from "@/locales";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export interface SignupState {
  error: string | null;
}

// See login/actions.ts's resolveLocale — same reasoning.
function resolveLocale(formData: FormData) {
  const raw = String(formData.get("locale") ?? "");
  return isLocale(raw) ? raw : DEFAULT_LOCALE;
}

async function signupBackendHeaders(): Promise<Record<string, string>> {
  // Product signup is Browser → Caddy → Next.js → backend. The ASGI peer is
  // the frontend container unless we forward Caddy's client IP (issue #190).
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

// Registration is backend-mediated (Ring 1-B design doc §6.5 decision 3a):
// the invite is redeemed and the Auth-provider account is created server-
// side by our own FastAPI backend, not by calling Supabase directly from
// here — that keeps invite-gating on our server, which is the whole point.
export async function signup(
  _prevState: SignupState | undefined,
  formData: FormData,
): Promise<SignupState | undefined> {
  const inviteToken = String(formData.get("invite_token") ?? "").trim();
  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");
  // The checkbox's own "on"/absent value — SignupForm already blocks
  // submission client-side when unchecked (Ring 1-Onboarding.md §2.5); the
  // backend's Literal[True] is the independent second layer.
  const tosAccepted = formData.get("tos_accepted") === "on";
  const auth = catalogs[resolveLocale(formData)].auth;

  if (!inviteToken) {
    return { error: auth.errorMissingInviteToken };
  }
  if (!email || password.length < 8) {
    return { error: auth.errorInvalidSignupInput };
  }

  const res = await fetch(`${BACKEND_URL}/auth/signup`, {
    method: "POST",
    headers: await signupBackendHeaders(),
    body: JSON.stringify({
      invite_token: inviteToken,
      email,
      password,
      tos_accepted: tosAccepted,
    }),
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
    return { error: detail ?? auth.signupThrownError };
  }

  // The backend's SignupResponse carries no session token (it only proves
  // the account was created) — sign in immediately with the same
  // credentials so sign-up is one step for the user, not two.
  const supabase = await createClient();
  const { error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) {
    redirect("/login");
  }

  // Onboarding entry point (Ring 1-Onboarding.md §2.1): the ONLY place
  // mode="onboarding" is ever triggered from.
  redirect("/questionnaire?onboarding=1");
}
