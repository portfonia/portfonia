"use server";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export interface ConfirmEmailVerificationState {
  error: string | null;
  // undefined until the backend has actually answered.
  email?: string;
}

// The one state-changing step (design doc §3.3 step 4) — everything before
// this (the GET status lookup in page.tsx, the Altcha challenge fetch) is
// inert. No IP/session context to forward here (contrast forgot-password's
// actions.ts, whose backend endpoint rate-limits by IP): the confirm
// endpoint isn't rate-limited yet — see docs/mechanisms or the design doc's
// note that the only caller able to CREATE a record today is the
// ADMIN_API_TOKEN-gated Ops API, so there's no untrusted-facing surface to
// flood this one with guesses against (the token is 128 bits regardless).
export async function confirmEmailVerification(
  _prevState: ConfirmEmailVerificationState | undefined,
  formData: FormData,
): Promise<ConfirmEmailVerificationState | undefined> {
  const token = String(formData.get("token") ?? "");
  const altchaPayload = String(formData.get("altcha") ?? "");

  if (!token) {
    return { error: "invalidOrExpired" };
  }
  if (!altchaPayload) {
    return { error: "genericError" };
  }

  const res = await fetch(`${BACKEND_URL}/email-verifications/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, altcha: altchaPayload }),
  });

  if (!res.ok) {
    return { error: res.status === 400 ? "invalidOrExpired" : "genericError" };
  }

  const body = (await res.json()) as { email: string };
  return { error: null, email: body.email };
}
