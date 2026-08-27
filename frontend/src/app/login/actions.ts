"use server";

import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";
import { catalogs, DEFAULT_LOCALE, isLocale } from "@/locales";

export interface LoginState {
  error: string | null;
}

// Server Actions have no request-scoped locale (no URL-based i18n routing —
// see src/locales/README.md); the client form submits its current locale
// state as a plain hidden field instead (login-form.tsx).
function resolveLocale(formData: FormData) {
  const raw = String(formData.get("locale") ?? "");
  return isLocale(raw) ? raw : DEFAULT_LOCALE;
}

export async function login(
  _prevState: LoginState | undefined,
  formData: FormData,
): Promise<LoginState | undefined> {
  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");
  const auth = catalogs[resolveLocale(formData)].auth;

  if (!email || !password) {
    return { error: auth.errorMissingCredentials };
  }

  const supabase = await createClient();
  const { error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) {
    // Never surface the Auth provider's own error text (e.g. "invalid_grant")
    // to the client — it can distinguish "no such account" from "wrong
    // password" for an attacker probing emails.
    return { error: auth.errorInvalidCredentials };
  }

  redirect("/holdings");
}
