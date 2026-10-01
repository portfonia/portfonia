"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { ApiError, cancelSubscription, getSubscriptionQuote, setSubscription, type Subscription, type SubscriptionQuote, type SubscriptionType } from "@/lib/api";

export type SubscriptionDialogState = { kind: "plan"; quote: SubscriptionQuote } | { kind: "cancel"; expires_on: string | null };

export function useSubscription(subscription: Subscription | undefined) {
  const t = useTranslations("profile");
  const router = useRouter();
  const [dialog, setDialog] = useState<SubscriptionDialogState | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function locked() {
    const remaining = subscription?.next_adjustment_at
      ? new Date(subscription.next_adjustment_at).getTime() - Date.now() : 0;
    if (remaining <= 0) return false;
    const minutes = Math.ceil(remaining / 60000);
    setError(t("subscriptionDailyLock", { hours: Math.floor(minutes / 60), minutes: minutes % 60 }));
    return true;
  }

  async function choose(value: SubscriptionType | "cancel") {
    if (pending) return;
    setError(null);
    if (locked()) return;
    const type = value === "cancel" ? subscription?.type : value;
    if (!type) return;
    setPending(true);
    try {
      const quote = await getSubscriptionQuote(type);
      if (value === "cancel") setDialog({ kind: "cancel", expires_on: quote.expires_on });
      else if (quote.action !== "none") setDialog({ kind: "plan", quote });
    } catch {
      setError(t("subscriptionError"));
    } finally {
      setPending(false);
    }
  }

  async function confirm() {
    if (!dialog || pending || (dialog.kind === "plan" && !dialog.quote.sufficient)) return;
    setError(null);
    setPending(true);
    try {
      if (dialog.kind === "cancel") await cancelSubscription();
      else await setSubscription(dialog.quote.type);
      setDialog(null);
      router.refresh();
    } catch (err) {
      const code = err instanceof ApiError && err.status === 409 ? err.message : "";
      switch (code) {
        case "daily_limit": case "email_unverified": case "insufficient_credits": case "no_change": case "no_subscription":
          setError(t(`subscriptionErrors.${code}`)); break;
        default: setError(t("subscriptionError"));
      }
    } finally {
      setPending(false);
    }
  }

  function close() {
    if (pending) return;
    setDialog(null);
    setError(null);
  }
  return { dialog, pending, error, choose, confirm, close };
}
