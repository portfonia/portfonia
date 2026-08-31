"use client";

import { useActionState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { confirmUnsubscribe, type ConfirmUnsubscribeState } from "./actions";
import type { UnsubscribeStatus } from "./page";

interface Props {
  token: string;
  status: UnsubscribeStatus;
}

export function UnsubscribeForm({ token, status }: Props) {
  const t = useTranslations("unsubscribe");

  const [state, formAction, pending] = useActionState<
    ConfirmUnsubscribeState | undefined,
    FormData
  >(confirmUnsubscribe, undefined);

  if (state?.email) {
    return (
      <div className="flex flex-col gap-3 text-center text-sm text-foreground/80" role="status">
        <p>{t("successBody", { email: state.email })}</p>
        <a href="/profile" className="underline">
          {t("setAddressLink")}
        </a>
      </div>
    );
  }

  if (!status.found) {
    return (
      <p className="text-center text-sm text-destructive" role="alert">
        {t("invalidOrExpired")}
      </p>
    );
  }

  return (
    <>
      <h1 className="text-xl font-medium">{t("heading")}</h1>
      <p className="text-sm text-foreground/80">{t("subtitle", { email: status.email ?? "" })}</p>
      <form action={formAction} className="flex flex-col gap-4">
        <input type="hidden" name="token" value={token} />
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
