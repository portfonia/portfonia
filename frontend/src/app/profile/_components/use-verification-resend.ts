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
      setPendingId(null);
    }
  }

  return { pendingId, error, handleResend };
}
