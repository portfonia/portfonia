"use client";

import Script from "next/script";
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AlertDialog, AlertDialogContent, AlertDialogHeader, AlertDialogTitle, AlertDialogDescription, AlertDialogFooter, AlertDialogCancel } from "@/components/ui/alert-dialog";
import { logoutAfterAccountDeletion } from "@/lib/auth-actions";
import { isNextRedirectError } from "@/lib/next-redirect-error";

interface DeletionSummary {
  cash_balance: string;
  refundable_cash: string;
  gift_balance: string;
  subscription_active: boolean;
}

function DeletionProof({ onChange }: { onChange: (payload: string | null) => void }) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    const widget = ref.current;
    function changed(event: Event) {
      const detail = (event as CustomEvent<{ state: string; payload?: string }>).detail;
      onChange(detail?.state === "verified" && detail.payload ? detail.payload : null);
    }
    widget?.addEventListener("statechange", changed);
    return () => widget?.removeEventListener("statechange", changed);
  }, [onChange]);
  return <>
    <Script src="/altcha.js" type="module" strategy="afterInteractive" />
    <altcha-widget ref={ref} challengeurl="/api/me/account-deletion/altcha-challenge" name="altcha" />
  </>;
}

export function DeleteAccountDialog({ email }: { email: string }) {
  const t = useTranslations("profile");
  const [summary, setSummary] = useState<DeletionSummary | null>(null);
  const [step, setStep] = useState<"confirm" | "relinquish">("confirm");
  const [signature, setSignature] = useState("");
  const [proof, setProof] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function discard() {
    setSummary(null);
    setStep("confirm");
    setSignature("");
    setProof(null);
  }
  async function start() {
    if (pending) return;
    discard();
    setError(null);
    setPending(true);
    try {
      const response = await fetch("/api/me/account-deletion");
      if (!response.ok) throw new Error("account deletion summary unavailable");
      setSummary(await response.json() as DeletionSummary);
    } catch {
      setError(t("errorLoadFailed"));
    } finally {
      setPending(false);
    }
  }
  const matched = signature.trim().toLowerCase() === email.trim().toLowerCase();
  async function confirm() {
    if (!summary || pending || !matched) return;
    if (step === "confirm" && Number(summary.cash_balance) > 0) {
      setSignature("");
      setProof(null);
      setStep("relinquish");
      return;
    }
    if (step === "relinquish" && !proof) return;
    setPending(true);
    setError(null);
    try {
      const response = await fetch("/api/me/account-deletion", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm_email: signature, relinquish_cash: summary.cash_balance, altcha: proof }),
      });
      if (response.status === 204) {
        discard();
        await logoutAfterAccountDeletion();
        return;
      }
      const data: unknown = await response.json();
      const detail = data && typeof data === "object" && "detail" in data ? data.detail : null;
      if (response.status === 409 && detail === "balance_changed") {
        setError(t("deleteAccountBalanceChanged"));
        discard();
      } else if (response.status === 400 && detail === "invalid captcha") {
        setError(t("deleteAccountInvalidCaptcha"));
        discard();
      } else {
        setError(t(response.status === 500 ? "deleteAccountIncomplete" : "deleteAccountFailed"));
      }
    } catch (error) {
      if (isNextRedirectError(error)) throw error;
      setError(t("deleteAccountFailed"));
    } finally {
      setPending(false);
    }
  }
  const relinquishing = step === "relinquish";
  const hasCash = summary !== null && Number(summary.cash_balance) > 0;
  return <>
    <Button variant="destructive" disabled={pending} onClick={() => void start()}>{t("deleteAccountHeading")}</Button>
    {!summary && error && <p role="alert" className="mt-2 text-sm text-destructive">{error}</p>}
    <AlertDialog open={summary !== null} onOpenChange={(open) => { if (!open && !pending) discard(); }}>
      {summary && <AlertDialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-lg">
        <AlertDialogHeader>
          <AlertDialogTitle>{t("deleteAccountHeading")}</AlertDialogTitle>
          <AlertDialogDescription render={<div />} className="space-y-2">
            {relinquishing ? <>
              <p>{t("deleteAccountPurchased", { cash_balance: summary.cash_balance })}</p>
              {Number(summary.refundable_cash) > 0 && <p>{t("deleteAccountRefundable", { refundable_cash: summary.refundable_cash })}</p>}
              <p>{t("deleteAccountVoluntary")}</p>
            </> : <>
              <p>{t("deleteAccountDisclosure")}</p>
              {summary.subscription_active && <p>{t("deleteAccountSubscription")}</p>}
              <p>{t("deleteAccountTokens")}</p>
              {Number(summary.gift_balance) > 0 && <p>{t("deleteAccountGift", { gift_balance: summary.gift_balance })}</p>}
              <p>{t("deleteAccountReregister")}</p>
            </>}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="space-y-2">
          <label htmlFor="deletion-email" className="text-sm font-medium">{t(relinquishing ? "deleteAccountSignatureLabel" : "deleteAccountEmailLabel")}</label>
          <Input id="deletion-email" type="text" autoComplete="off" value={signature} disabled={pending} onChange={(event) => setSignature(event.target.value)} />
        </div>
        {relinquishing && <DeletionProof onChange={setProof} />}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending} onClick={discard}>{t("subscriptionDismiss")}</AlertDialogCancel>
          <Button variant="destructive" disabled={pending || !matched || (relinquishing && !proof)} onClick={() => void confirm()}>
            {relinquishing ? t("deleteAccountRelinquish", { cash_balance: summary.cash_balance }) : t(hasCash ? "deleteAccountContinue" : "deleteAccountHeading")}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>}
    </AlertDialog>
  </>;
}
