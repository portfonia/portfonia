"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { createClient } from "@/lib/supabase/browser";

// Consumption side of issue #231's forgot-password flow — client-direct to
// Supabase, same privilege level as login (no PoW, no backend involvement,
// no Bearer token: see docs/mechanisms/identity-and-auth.md's issue #190
// section). Supabase's own recovery link puts a session in the URL, which
// supabase-js's browser client detects automatically on load
// (detectSessionInUrl, the default) — by the time this component mounts,
// updateUser({ password }) already has a session to act on. If the link
// was invalid/expired, that call itself fails and reports the generic error
// below; there is no separate "is this link valid" pre-check, matching
// KISS over building a bespoke state machine for an edge case Supabase's
// own call already reports.
export function ResetPasswordForm() {
  const t = useTranslations("auth");
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [mismatch, setMismatch] = useState(false);
  const [pending, setPending] = useState(false);
  const [success, setSuccess] = useState(false);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const password = String(form.get("password") ?? "");
    const confirmation = String(form.get("confirm_password") ?? "");

    if (password !== confirmation) {
      setMismatch(true);
      return;
    }
    setMismatch(false);
    setError(null);
    setPending(true);

    const supabase = createClient();
    const { error: updateError } = await supabase.auth.updateUser({ password });

    setPending(false);
    if (updateError) {
      setError(t("resetPasswordFailed"));
      return;
    }
    setSuccess(true);
    // Supabase revokes other sessions after a password change (same
    // accepted side effect as profile/actions.ts's changePassword) — send
    // the user to log back in with the new password rather than pretending
    // this page can carry them straight into an authenticated route.
    setTimeout(() => router.push("/login"), 2000);
  }

  if (success) {
    return (
      <p className="text-center text-sm text-foreground/80" role="status">
        {t("resetPasswordSuccess")}
      </p>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="mx-auto flex max-w-sm flex-col gap-4">
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
      {mismatch ? (
        <p className="text-sm text-destructive" role="alert">
          {t("passwordMismatch")}
        </p>
      ) : error ? (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : null}
      <Button type="submit" disabled={pending}>
        {pending ? t("resetPasswordSubmitting") : t("resetPasswordSubmit")}
      </Button>
    </form>
  );
}
