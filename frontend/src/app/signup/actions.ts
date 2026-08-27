"use server";

import { redirect } from "next/navigation";

import { messages } from "@/lib/messages";
import { createClient } from "@/lib/supabase/server";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export interface SignupState {
  error: string | null;
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

  if (!inviteToken) {
    return { error: "This sign-up link is missing its invite token." };
  }
  if (!email || password.length < 8) {
    return { error: "Enter a valid email and a password of at least 8 characters." };
  }

  const res = await fetch(`${BACKEND_URL}/auth/signup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invite_token: inviteToken, email, password }),
  });

  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
    const detail = typeof body?.detail === "string" ? body.detail : null;
    if (res.status === 429) {
      return { error: detail ?? messages.auth.tooManyAttempts };
    }
    if (res.status === 503) {
      return { error: detail ?? messages.auth.temporarilyUnavailable };
    }
    return { error: detail ?? "Sign up failed." };
  }

  // The backend's SignupResponse carries no session token (it only proves
  // the account was created) — sign in immediately with the same
  // credentials so sign-up is one step for the user, not two.
  const supabase = await createClient();
  const { error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) {
    redirect("/login");
  }

  redirect("/holdings");
}
