"use client";

import { useActionState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useLocale } from "@/app/_components/locale-provider";
import { changePassword, type ChangePasswordState } from "../actions";

export function ChangePasswordForm() {
  const t = useTranslations("profile");
  const { locale } = useLocale();

  const [state, formAction, pending] = useActionState<ChangePasswordState | undefined, FormData>(
    changePassword,
    undefined,
  );

  return (
    <form action={formAction} className="flex flex-col gap-4">
      {/* Server Actions have no request-scoped locale (no URL-based i18n
          routing — see src/locales/README.md); the client form submits its
          current locale state as a plain hidden field, same as login/signup. */}
      <input type="hidden" name="locale" value={locale} />
      <div className="flex flex-col gap-1.5">
        <label htmlFor="currentPassword" className="text-sm text-foreground/80">
          {t("currentPasswordLabel")}
        </label>
        <Input
          id="currentPassword"
          name="currentPassword"
          type="password"
          autoComplete="current-password"
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="newPassword" className="text-sm text-foreground/80">
          {t("newPasswordLabel")}
        </label>
        <Input
          id="newPassword"
          name="newPassword"
          type="password"
          autoComplete="new-password"
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="confirmNewPassword" className="text-sm text-foreground/80">
          {t("confirmNewPasswordLabel")}
        </label>
        <Input
          id="confirmNewPassword"
          name="confirmNewPassword"
          type="password"
          autoComplete="new-password"
          required
        />
      </div>
      {state?.error && (
        <p className="text-sm text-destructive" role="alert">
          {state.error}
        </p>
      )}
      {state?.success && (
        <p className="text-sm text-foreground/80" role="status">
          {t("passwordChangeSuccess")}
        </p>
      )}
      <Button type="submit" disabled={pending} className="self-start">
        {pending ? t("changingPassword") : t("changePasswordButton")}
      </Button>
    </form>
  );
}
