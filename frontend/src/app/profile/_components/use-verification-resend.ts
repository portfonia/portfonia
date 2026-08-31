"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useTranslations } from "next-intl";

import { ApiError, resendEmailVerification } from "@/lib/api";

// Shared resend flow for the Profile page's two resend affordances (issue
// #262 §8.4 + issue #269 §6). A resend supersedes the old record
// server-side, so no local state is patched — success calls
// router.refresh() to re-render the page's server data (a fresh GET /me),
// which preserves client state the way a hard window.location.reload()
// would not. 429/503 carry user-readable wording (never raw status text),
// per the forgot-password error-state pattern.
export function useVerificationResend() {
  const t = useTranslations("profile");
  const router = useRouter();
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleResend(id: string) {
    setPendingId(id);
    setError(null);
    try {
      await resendEmailVerification(id);
      router.refresh();
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 429) {
          setError(t("emailVerificationResendTooSoon"));
        } else if (err.status === 503) {
          setError(t("emailVerificationResendUnavailable"));
        } else {
          setError(t("emailVerificationResendFailed"));
        }
      } else {
        setError(t("emailVerificationResendFailed"));
      }
    } finally {
      // Success path too: a completed resend must re-enable every resend
      // button on the page. Leaving the (superseded) record id set would
      // disable all of them until a full remount — the inline delivery-email
      // button then shows "Resend" yet stays disabled (PR #270 review
      // finding). The server-side 60s cooldown plus the 429 copy already
      // cover double-submit while the refresh is in flight.
      setPendingId(null);
    }
  }

  return { pendingId, error, handleResend };
}
