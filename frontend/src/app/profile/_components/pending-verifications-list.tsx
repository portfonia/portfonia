"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { resendEmailVerification, ApiError } from "@/lib/api";
import type { PendingEmailVerification } from "@/lib/api";

// Issue #262, Ring 1-Profile Page.md §8.4. A resend supersedes the old
// record server-side, so no local state is patched — success just refetches
// GET /me (via router.refresh()) and the list re-renders from the new
// record ids. 429/503 carry user-readable wording (never raw status text),
// per the forgot-password error-state pattern.
export function PendingVerificationsList({
  verifications,
}: {
  verifications: PendingEmailVerification[];
}) {
  const t = useTranslations("profile");
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleResend(id: string) {
    setPendingId(id);
    setError(null);
    try {
      await resendEmailVerification(id);
      window.location.reload();
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

  if (verifications.length === 0) return null;

  return (
    <div className="flex flex-col gap-3">
      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
      {verifications.map((item) => (
        <div key={item.id} className="flex flex-col gap-1">
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm">{item.email}</span>
            <Button
              variant="outline"
              disabled={pendingId !== null}
              onClick={() => void handleResend(item.id)}
            >
              {pendingId === item.id
                ? t("emailVerificationResending")
                : t("emailVerificationResendButton")}
            </Button>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span className={item.status === "undeliverable" ? "text-destructive" : "text-muted-foreground"}>
              {item.status === "undeliverable"
                ? t("emailVerificationStatusUndeliverable")
                : t("emailVerificationStatusPending")}
            </span>
            <span className="text-muted-foreground">
              {t("emailVerificationPurpose", {
                purpose:
                  item.purpose === "delivery_email"
                    ? t("emailVerificationPurposeDelivery")
                    : t("emailVerificationPurposeAccount"),
              })}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}
