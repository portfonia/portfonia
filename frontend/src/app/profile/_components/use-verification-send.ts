"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useTranslations } from "next-intl";

import { ApiError, createEmailVerification } from "@/lib/api";

// Issue #289, Profile Page.md §10: "Send verification" for an address
// already on record, used by the noVerifiedRecipient gap card. Unlike
// resend (which needs an existing pending/undeliverable record — §8.3),
// this creates a fresh record from the account's own known fields
// (purpose=account_email | delivery_email); the server never accepts an
// arbitrary address. Success calls router.refresh() to re-render the
// page's server data (a fresh GET /me) — same discipline as
// useVerificationResend: never window.location.reload().
export function useVerificationSend() {
  const t = useTranslations("profile");
  const router = useRouter();
  const [pendingPurpose, setPendingPurpose] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSend(purpose: "account_email" | "delivery_email") {
    setPendingPurpose(purpose);
    setError(null);
    try {
      await createEmailVerification(purpose);
      router.refresh();
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 429) {
          setError(t("emailVerificationSendTooSoon"));
        } else if (err.status === 503) {
          // Same fail-closed wording as the resend flow (Redis down).
          setError(t("emailVerificationResendUnavailable"));
        } else {
          setError(t("emailVerificationSendFailed"));
        }
      } else {
        setError(t("emailVerificationSendFailed"));
      }
    } finally {
      // Success path too: a completed send must re-enable every button on
      // the page (same reasoning as useVerificationResend's finally).
      setPendingPurpose(null);
    }
  }

  return { pendingPurpose, error, handleSend };
}
