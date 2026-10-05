"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { revalidateSession } from "@/hooks/use-session";
import { ApiError, cancelSubscription, getSubscriptionQuote, setSubscription, type Subscription, type SubscriptionQuote, type SubscriptionType } from "@/lib/api";

export type SubscriptionDialogState = { kind: "plan"; quote: SubscriptionQuote } | { kind: "cancel"; expires_on: string | null };

export function useSubscription(subscription: Subscription | undefined) {
  const t = useTranslations("profile");
  const router = useRouter();
  const [dialog, setDialog] = useState<SubscriptionDialogState | null>(null);
  const [pending, setPending] = useState(false);
  const [errorDesc, setErrorDesc] = useState<{ key: string; values?: Record<string, number> } | null>(null);
  const error = errorDesc ? t(errorDesc.key, errorDesc.values) : null;

  function locked() {
    const remaining = subscription?.next_adjustment_at
      ? new Date(subscription.next_adjustment_at).getTime() - Date.now() : 0;
    if (remaining <= 0) return false;
    const minutes = Math.ceil(remaining / 60000);
    setErrorDesc({ key: "subscriptionDailyLock", values: { hours: Math.floor(minutes / 60), minutes: minutes % 60 } });
    return true;
  }

  async function choose(value: SubscriptionType | "cancel") {
    if (pending) return;
    setErrorDesc(null);
    if (locked()) return;
    const type = value === "cancel" ? subscription?.type : value;
    if (!type) return;
    setPending(true);
    try {
      const quote = await getSubscriptionQuote(type);
      if (value === "cancel") setDialog({ kind: "cancel", expires_on: quote.expires_on });
      else if (quote.action !== "none") setDialog({ kind: "plan", quote });
    } catch {
      setErrorDesc({ key: "subscriptionError" });
    } finally {
      setPending(false);
    }
  }

  async function confirm() {
    if (!dialog || pending || (dialog.kind === "plan" && !dialog.quote.sufficient)) return;
    setErrorDesc(null);
    setPending(true);
    try {
      if (dialog.kind === "cancel") await cancelSubscription();
      else await setSubscription(dialog.quote.type);
      setDialog(null);
      router.refresh();
      revalidateSession();
    } catch (err) {
      const code = err instanceof ApiError && err.status === 409 ? err.message : "";
      switch (code) {
        case "daily_limit": case "email_unverified": case "insufficient_credits": case "no_change": case "no_subscription":
          setErrorDesc({ key: `subscriptionErrors.${code}` }); break;
        default: setErrorDesc({ key: "subscriptionError" });
      }
    } finally {
      setPending(false);
    }
  }

  function close() {
    if (pending) return;
    setDialog(null);
    setErrorDesc(null);
  }
  return { dialog, pending, error, choose, confirm, close };
}
