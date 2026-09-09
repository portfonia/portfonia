"use server";

import { createClient, currentAccessToken } from "@/lib/supabase/server";
import { catalogs, DEFAULT_LOCALE, isLocale } from "@/locales";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export interface ChangePasswordState {
  error: string | null;
  success: boolean;
}

// See login/actions.ts's resolveLocale — same reasoning (no URL-based
// locale routing; the client form submits its current locale state).
function resolveLocale(formData: FormData) {
  const raw = String(formData.get("locale") ?? "");
  return isLocale(raw) ? raw : DEFAULT_LOCALE;
}

async function verifyChangePasswordAltcha(payload: string): Promise<boolean> {
  const token = await currentAccessToken();
  if (!token) return false;
  try {
    const res = await fetch(`${BACKEND_URL}/me/change-password/altcha-verify`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ altcha: payload }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

// Ring 1-Profile Page.md decision 2: signInWithPassword(email, current
// password) IS the current-password check, then updateUser({ password })
// changes it. Issue #393: Altcha PoW is verified against the backend
// first; a missing/invalid payload never reaches the Auth provider.
export async function changePassword(
  _prevState: ChangePasswordState | undefined,
  formData: FormData,
): Promise<ChangePasswordState | undefined> {
  const currentPassword = String(formData.get("currentPassword") ?? "");
  const newPassword = String(formData.get("newPassword") ?? "");
  const confirmNewPassword = String(formData.get("confirmNewPassword") ?? "");
  const altchaPayload = String(formData.get("altcha") ?? "");
  const t = catalogs[resolveLocale(formData)].profile;

  if (!currentPassword || !newPassword || !confirmNewPassword) {
    return { error: t.errorMissingFields, success: false };
  }
  if (newPassword.length < 8) {
    return { error: t.passwordTooShort, success: false };
  }
  if (newPassword !== confirmNewPassword) {
    return { error: t.passwordMismatch, success: false };
  }
  if (!altchaPayload) {
    return { error: t.errorCaptchaRequired, success: false };
  }
  if (!(await verifyChangePasswordAltcha(altchaPayload))) {
    return { error: t.errorCaptchaRequired, success: false };
  }

  const supabase = await createClient();

  // The account to verify against comes from the caller's own session, not
  // a client-submitted field — a forged/stale email in the form must never
  // be able to steer which account's password gets checked or changed.
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user?.email) {
    return { error: t.errorPasswordChangeFailed, success: false };
  }

  const { error: verifyError } = await supabase.auth.signInWithPassword({
    email: user.email,
    password: currentPassword,
  });
  if (verifyError) {
    return { error: t.errorCurrentPasswordIncorrect, success: false };
  }

  const { error: updateError } = await supabase.auth.updateUser({ password: newPassword });
  if (updateError) {
    return { error: t.errorPasswordChangeFailed, success: false };
  }

  return { error: null, success: true };
}
