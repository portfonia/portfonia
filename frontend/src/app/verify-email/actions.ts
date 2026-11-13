"use server";

import { forwardedClientHeaders } from "@/lib/server-api";

import { redirect } from "next/navigation";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export interface ConfirmEmailVerificationState {
  error: string | null;
  // undefined until the backend has actually answered.
  email?: string;
}

// The one state-changing step (design doc §3.3 step 4) — everything before
// this (the GET status lookup in page.tsx, the Altcha challenge fetch) is
// inert. Browser IP and user agent are forwarded for the activity log (#884).
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
    headers: { "Content-Type": "application/json", ...(await forwardedClientHeaders()) },
    body: JSON.stringify({ token, altcha: altchaPayload }),
  });

  if (!res.ok) {
    return { error: res.status === 400 ? "invalidOrExpired" : "genericError" };
  }

  redirect("/profile");
}
