"use client";

import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import type { PendingEmailVerification } from "@/lib/api";
import { useVerificationResend } from "./use-verification-resend";

// Issue #262, Ring 1-Profile Page.md §8.4. A resend supersedes the old
// record server-side, so no local state is patched — success calls
// router.refresh() to re-render the page's server data (a fresh GET /me),
// which preserves client state the way a hard window.location.reload()
// would not (see use-verification-resend.ts, shared with the delivery-email
// section's inline resend, issue #269 §6).
export function PendingVerificationsList({
  verifications,
}: {
  verifications: PendingEmailVerification[];
}) {
  const t = useTranslations("profile");
  const { pendingId, error, handleResend } = useVerificationResend();

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
