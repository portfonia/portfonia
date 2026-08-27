"use client";

import { useActionState, useState, type FormEvent } from "react";

import { messages } from "@/lib/messages";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { markPendingLogin } from "@/hooks/use-session";
import { settleAuthAction } from "@/lib/settle-auth-action";
import { signup, type SignupState } from "./actions";

const m = messages.auth;

async function signupAndDisarmOnError(
  prev: SignupState | undefined,
  formData: FormData,
): Promise<SignupState | undefined> {
  return settleAuthAction(() => signup(prev, formData), "Sign up failed.");
}

export function SignupForm({ inviteToken }: { inviteToken: string }) {
  const [state, formAction, pending] = useActionState<SignupState | undefined, FormData>(
    signupAndDisarmOnError,
    undefined,
  );
  const [mismatch, setMismatch] = useState(false);

  if (!inviteToken) {
    return (
      <p className="mx-auto max-w-sm text-center text-sm text-destructive" role="alert">
        {m.missingInvite}
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
    markPendingLogin();
  }

  return (
    <form
      action={formAction}
      onSubmit={handleSubmit}
      className="mx-auto flex max-w-sm flex-col gap-4"
    >
      <input type="hidden" name="invite_token" value={inviteToken} />
      <div className="flex flex-col gap-1.5">
        <label htmlFor="email" className="text-sm text-foreground/80">
          {m.emailLabel}
        </label>
        <Input id="email" name="email" type="email" autoComplete="email" required />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="password" className="text-sm text-foreground/80">
          {m.passwordLabel}
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
          {m.confirmPasswordLabel}
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
          {m.passwordMismatch}
        </p>
      ) : state?.error ? (
        <p className="text-sm text-destructive" role="alert">
          {state.error}
        </p>
      ) : null}
      <Button type="submit" disabled={pending}>
        {pending ? m.signingUp : m.signupButton}
      </Button>
    </form>
  );
}
