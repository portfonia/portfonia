"use client";

import { useActionState } from "react";
import Script from "next/script";
import { useTranslations } from "next-intl";

import { useLocale } from "@/app/_components/locale-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { submitWaitlist, type WaitlistState } from "./actions";

export function WaitlistForm() {
  const t = useTranslations("auth");
  const { locale } = useLocale();
  const [state, action, pending] = useActionState<WaitlistState | undefined, FormData>(
    submitWaitlist,
    undefined,
  );

  if (state?.received) {
    return <p className="text-center text-sm text-foreground/80" role="status">{t("waitlistReceived")}</p>;
  }

  return (
    <form action={action} className="mx-auto flex max-w-sm flex-col gap-4">
      <input type="hidden" name="locale" value={locale} />
      <div className="flex flex-col gap-1.5">
        <label htmlFor="waitlist-email" className="text-sm text-foreground/80">
          {t("emailLabel")}
        </label>
        <Input id="waitlist-email" name="email" type="email" autoComplete="email" required />
      </div>
      <Script src="/altcha.js" type="module" strategy="afterInteractive" />
      <altcha-widget challengeurl="/api/waitlist/altcha-challenge" name="altcha" />
      {state?.error && <p className="text-sm text-destructive" role="alert">{state.error}</p>}
      <Button type="submit" disabled={pending}>
        {pending ? t("waitlistSubmitting") : t("waitlistSubmit")}
      </Button>
    </form>
  );
}
