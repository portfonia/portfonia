"use client";

import { useActionState } from "react";

import { messages } from "@/lib/messages";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { clearPendingLogin, markPendingLogin } from "@/hooks/use-session";
import { signup, type SignupState } from "./actions";

const m = messages.auth;

async function signupAndDisarmOnError(
  prev: SignupState | undefined,
  formData: FormData,
): Promise<SignupState | undefined> {
  const result = await signup(prev, formData);
  if (result?.error) clearPendingLogin();
  return result;
}

export function SignupForm({ inviteToken }: { inviteToken: string }) {
  const [state, formAction, pending] = useActionState<SignupState | undefined, FormData>(
    signupAndDisarmOnError,
    undefined,
  );

  if (!inviteToken) {
    return (
      <p className="mx-auto max-w-sm text-center text-sm text-destructive" role="alert">
        {m.missingInvite}
      </p>
    );
  }

  return (
    <form
      action={formAction}
      onSubmit={() => markPendingLogin()}
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
      {state?.error && (
        <p className="text-sm text-destructive" role="alert">
          {state.error}
        </p>
      )}
      <Button type="submit" disabled={pending}>
        {pending ? m.signingUp : m.signupButton}
      </Button>
    </form>
  );
}
