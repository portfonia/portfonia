"use client";

import { useActionState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useLocale } from "@/app/_components/locale-provider";
import { markPendingLogin } from "@/hooks/use-session";
import { settleAuthAction } from "@/lib/settle-auth-action";
import { login, type LoginState } from "./actions";

export function LoginForm() {
  const t = useTranslations("auth");
  const { locale } = useLocale();

  async function loginAndDisarmOnError(
    prev: LoginState | undefined,
    formData: FormData,
  ): Promise<LoginState | undefined> {
    return settleAuthAction(() => login(prev, formData), t("signinThrownError"));
  }

  const [state, formAction, pending] = useActionState<LoginState | undefined, FormData>(
    loginAndDisarmOnError,
    undefined,
  );

  return (
    <form
      action={formAction}
      onSubmit={() => markPendingLogin()}
      className="mx-auto flex max-w-sm flex-col gap-4"
    >
      {/* The login Server Action has no other way to know the visitor's
          selected locale (no URL-based routing — see src/locales/README.md),
          so the client-only locale state rides along as a plain form field. */}
      <input type="hidden" name="locale" value={locale} />
      <div className="flex flex-col gap-1.5">
        <label htmlFor="email" className="text-sm text-foreground/80">
          {t("emailLabel")}
        </label>
        <Input id="email" name="email" type="email" autoComplete="email" required />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="password" className="text-sm text-foreground/80">
          {t("passwordLabel")}
        </label>
        <Input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
        />
      </div>
      {state?.error && (
        <p className="text-sm text-destructive" role="alert">
          {state.error}
        </p>
      )}
      <Button type="submit" disabled={pending}>
        {pending ? t("loggingIn") : t("loginButton")}
      </Button>
      <p className="text-center text-xs text-foreground/60">{t("noAccountYet")}</p>
    </form>
  );
}
