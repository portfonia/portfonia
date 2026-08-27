"use client";

import { useActionState, useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useLocale } from "@/app/_components/locale-provider";
import { markPendingLogin } from "@/hooks/use-session";
import { settleAuthAction } from "@/lib/settle-auth-action";
import { signup, type SignupState } from "./actions";

export function SignupForm({ inviteToken }: { inviteToken: string }) {
  const t = useTranslations("auth");
  const { locale } = useLocale();

  async function signupAndDisarmOnError(
    prev: SignupState | undefined,
    formData: FormData,
  ): Promise<SignupState | undefined> {
    return settleAuthAction(() => signup(prev, formData), t("signupThrownError"));
  }

  const [state, formAction, pending] = useActionState<SignupState | undefined, FormData>(
    signupAndDisarmOnError,
    undefined,
  );
  const [mismatch, setMismatch] = useState(false);
  const [tosError, setTosError] = useState(false);

  if (!inviteToken) {
    return (
      <p className="mx-auto max-w-sm text-center text-sm text-destructive" role="alert">
        {t("missingInvite")}
      </p>
    );
  }

  function handleSubmit(e: FormEvent<HTMLFormElement>) {
    const form = new FormData(e.currentTarget);
    const password = String(form.get("password") ?? "");
    const confirmation = String(form.get("confirm_password") ?? "");
    if (password !== confirmation) {
      // Block the action entirely: the pending-login signal is only disarmed
      // after the action runs, so arming it on a client-side rejection would
      // leave it stuck.
      e.preventDefault();
      setMismatch(true);
      return;
    }
    setMismatch(false);
    // Client-side half of the two-layer ToS gate (Ring 1-Onboarding.md §2.5)
    // — the backend's SignupRequest.tos_accepted (Literal[True]) is the
    // other, independent layer. An unchecked checkbox is simply absent from
    // FormData, not "off".
    if (form.get("tos_accepted") !== "on") {
      e.preventDefault();
      setTosError(true);
      return;
    }
    setTosError(false);
    markPendingLogin();
  }

  return (
    <form
      action={formAction}
      onSubmit={handleSubmit}
      className="mx-auto flex max-w-sm flex-col gap-4"
    >
      <input type="hidden" name="invite_token" value={inviteToken} />
      {/* See login-form.tsx: the signup Server Action needs the visitor's
          locale and has no other way to get it. */}
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
          autoComplete="new-password"
          minLength={8}
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="confirm-password" className="text-sm text-foreground/80">
          {t("confirmPasswordLabel")}
        </label>
        <Input
          id="confirm-password"
          name="confirm_password"
          type="password"
          autoComplete="new-password"
          minLength={8}
          required
        />
      </div>
      <div className="flex items-center gap-2">
        <input
          id="tos"
          name="tos_accepted"
          type="checkbox"
          className="h-4 w-4 rounded border-input"
        />
        <label htmlFor="tos" className="text-sm text-foreground/80">
          {t("tosLabel")}
        </label>
      </div>

      {mismatch ? (
        <p className="text-sm text-destructive" role="alert">
          {t("passwordMismatch")}
        </p>
      ) : tosError ? (
        <p className="text-sm text-destructive" role="alert">
          {t("tosRequired")}
        </p>
      ) : state?.error ? (
        <p className="text-sm text-destructive" role="alert">
          {state.error}
        </p>
      ) : null}
      <Button type="submit" disabled={pending}>
        {pending ? t("signingUp") : t("signupButton")}
      </Button>
    </form>
  );
}
