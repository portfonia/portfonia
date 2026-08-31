"use server";

import { redirect } from "next/navigation";

import { isNextRedirectError } from "@/lib/next-redirect-error";
import { getMeServer } from "@/lib/server-api";
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

  // Issue #280 item 3 (product decision): a returning user whose onboarding
  // is complete (no questionnaire/holdings gaps) lands on /profile — the
  // nav's Home-entry replacement; anyone still mid-onboarding keeps the
  // /holdings landing so the flow's next step stays reachable. getMeServer's
  // 401 path logout()s (its own redirect throw) and must propagate; any
  // other failure after a successful sign-in degrades to the old landing.
  let destination = "/holdings";
  try {
    const me = await getMeServer();
    destination = me.missing.length === 0 ? "/profile" : "/holdings";
  } catch (err) {
    if (isNextRedirectError(err)) throw err;
  }
  redirect(destination);
}
