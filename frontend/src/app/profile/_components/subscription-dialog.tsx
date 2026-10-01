"use client";

import { useTranslations } from "next-intl";
import { AlertDialog, AlertDialogContent, AlertDialogHeader, AlertDialogTitle, AlertDialogDescription, AlertDialogFooter, AlertDialogAction, AlertDialogCancel } from "@/components/ui/alert-dialog";
import type { useSubscription } from "./use-subscription";

export function SubscriptionDialog({ state }: { state: ReturnType<typeof useSubscription> }) {
  const t = useTranslations("profile");
  const dialog = state.dialog;
  if (!dialog) return null;
  const quote = dialog.kind === "plan" ? dialog.quote : null;
  // Display the server's instant in ET; do not derive a report date or cadence.
  const firstReport = quote ? new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(new Date(quote.first_report_at)).replace(",", "") + " ET" : "";
  return (
    <AlertDialog open onOpenChange={(open) => { if (!open) state.close(); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{quote ? t(`subscriptionTitle.${quote.type}`) : t("subscriptionCancel")}</AlertDialogTitle>
          <AlertDialogDescription render={<div />}>
            {quote ? <>
              <p>{t(`subscriptionDescription.${quote.type}`)}</p>
              {quote.action === "resume" ? <p>{t("subscriptionContinues", { expires_on: quote.expires_on ?? "" })}</p> : <>
                <p>{t("subscriptionFee", { fee: quote.fee })}</p>
                <p>{t("subscriptionBalance", { balance: quote.balance })}</p>
              </>}
              {quote.action === "change" && <>
                <p>{t("subscriptionReturned", { returned: quote.returned })}</p>
                <p>{t("subscriptionStartsToday")}</p>
              </>}
              <p>{t("subscriptionBalanceAfter", { balance_after: quote.balance_after })}</p>
              <p>{t("subscriptionPaidThrough", { expires_on: quote.expires_on ?? "" })}</p>
              <p>{t("subscriptionFirstReport", { first_report_at: firstReport })}</p>
              {quote.needs_holdings && <p>{t("subscriptionNeedsHoldings")}</p>}
              <p>{t("subscriptionDailyRule")}</p>
              {!quote.sufficient && <p>{t("subscriptionTopUp")}</p>}
            </> : <>
              <p>{t("subscriptionCancelBody", { expires_on: dialog.kind === "cancel" ? dialog.expires_on ?? "" : "" })}</p>
              <p>{t("subscriptionDailyRule")}</p>
            </>}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {state.error && <p className="text-sm text-destructive" role="alert">{state.error}</p>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={state.pending} onClick={state.close}>{t(quote ? "subscriptionDismiss" : "subscriptionKeep")}</AlertDialogCancel>
          <AlertDialogAction disabled={state.pending || (quote !== null && !quote.sufficient)} onClick={() => void state.confirm()}>
            {t(quote ? "subscriptionConfirm" : "subscriptionConfirmCancellation")}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
