"use client";

import { useActionState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { AltchaWidget } from "./_components/altcha-widget";
import { confirmEmailVerification, type ConfirmEmailVerificationState } from "./actions";
import type { VerifyEmailStatus } from "./page";

interface Props {
  token: string;
  status: VerifyEmailStatus;
}

// Statuses that mean "there is nothing left to confirm" — same generic
// message for all of them (design doc §3.3 step 4: never help a visitor
// distinguish expired from already-used from tampered).
const TERMINAL_NON_PENDING_STATUSES = new Set(["expired", "superseded", "undeliverable"]);

export function VerifyEmailForm({ token, status }: Props) {
  const t = useTranslations("emailVerification");

  const [state, formAction, pending] = useActionState<
    ConfirmEmailVerificationState | undefined,
    FormData
  >(confirmEmailVerification, undefined);

  if (state?.email) {
    return (
      <p className="text-center text-sm text-foreground/80" role="status">
        {t("successMessage", { email: state.email })}
      </p>
    );
  }

  if (!status.found || TERMINAL_NON_PENDING_STATUSES.has(status.status ?? "")) {
    return (
      <p className="text-center text-sm text-destructive" role="alert">
        {t("invalidOrExpired")}
      </p>
    );
  }

  if (status.status === "verified") {
    return (
      <p className="text-center text-sm text-foreground/80" role="status">
        {t("successMessage", { email: status.email ?? "" })}
      </p>
    );
  }

  return (
    <>
      <h1 className="text-xl font-medium">{t("heading")}</h1>
      <p className="text-sm text-foreground/80">{t("subtitle", { email: status.email ?? "" })}</p>
      <form action={formAction} className="flex flex-col gap-4">
        <input type="hidden" name="token" value={token} />
        <AltchaWidget />
        {state?.error && (
          <p className="text-sm text-destructive" role="alert">
            {t(state.error as "invalidOrExpired" | "genericError")}
          </p>
        )}
        <Button type="submit" disabled={pending}>
          {pending ? t("confirming") : t("confirmButton")}
        </Button>
      </form>
    </>
  );
}
