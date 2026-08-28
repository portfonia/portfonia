"use client";

import { useActionState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useLocale } from "@/app/_components/locale-provider";
import { AltchaWidget } from "./_components/altcha-widget";
import { forgotPassword, type ForgotPasswordState } from "./actions";

export function ForgotPasswordForm() {
  const t = useTranslations("auth");
  const { locale } = useLocale();

  const [state, formAction, pending] = useActionState<ForgotPasswordState | undefined, FormData>(
    forgotPassword,
    undefined,
  );

  // Once the backend has answered, replace the form with the explicit
  // found/not-found message (issue #231's deliberate departure from OWASP
  // enumeration-resistance guidance — see docs/mechanisms/identity-and-auth.md).
  if (state?.accountFound !== undefined) {
    return (
      <p className="text-center text-sm text-foreground/80" role="status">
        {state.accountFound
          ? t("forgotPasswordEmailSent")
          : t("forgotPasswordAccountNotFound")}
      </p>
    );
  }

  return (
    <form action={formAction} className="mx-auto flex max-w-sm flex-col gap-4">
      {/* See login/actions.ts's resolveLocale: the Server Action needs the
          visitor's locale and has no other way to get it. */}
      <input type="hidden" name="locale" value={locale} />
      <div className="flex flex-col gap-1.5">
        <label htmlFor="email" className="text-sm text-foreground/80">
          {t("emailLabel")}
        </label>
        <Input id="email" name="email" type="email" autoComplete="email" required />
      </div>
      <AltchaWidget />
      {state?.error && (
        <p className="text-sm text-destructive" role="alert">
          {state.error}
        </p>
      )}
      <Button type="submit" disabled={pending}>
        {pending ? t("forgotPasswordSubmitting") : t("forgotPasswordSubmit")}
      </Button>
    </form>
  );
}
