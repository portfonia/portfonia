"use server";

import { headers } from "next/headers";

import { catalogs, DEFAULT_LOCALE, isLocale } from "@/locales";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export interface WaitlistState {
  error: string | null;
  received?: boolean;
}

export async function submitWaitlist(
  _prevState: WaitlistState | undefined,
  formData: FormData,
): Promise<WaitlistState> {
  const rawLocale = String(formData.get("locale") ?? "");
  const locale = isLocale(rawLocale) ? rawLocale : DEFAULT_LOCALE;
  const email = String(formData.get("email") ?? "").trim();
  const altcha = String(formData.get("altcha") ?? "");
  const copy = catalogs[locale].auth;
  if (!email) return { error: copy.waitlistEmailRequired };
  if (!altcha) return { error: copy.waitlistCaptchaRequired };

  const incoming = await headers();
  const requestHeaders: Record<string, string> = { "Content-Type": "application/json" };
  const xff = incoming.get("x-forwarded-for");
  const realIp = incoming.get("x-real-ip");
  if (xff) requestHeaders["X-Forwarded-For"] = xff;
  if (realIp) requestHeaders["X-Real-IP"] = realIp;

  try {
    const response = await fetch(`${BACKEND_URL}/waitlist`, {
      method: "POST",
      headers: requestHeaders,
      body: JSON.stringify({ email, locale, altcha }),
    });
    if (response.ok) return { error: null, received: true };
    if (response.status === 400) return { error: copy.waitlistCaptchaRequired };
    if (response.status === 429) return { error: copy.tooManyAttempts };
    if (response.status === 503) return { error: copy.temporarilyUnavailable };
    return { error: copy.waitlistError };
  } catch {
    return { error: copy.waitlistError };
  }
}
