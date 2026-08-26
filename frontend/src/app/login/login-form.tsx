"use client";

import { useActionState } from "react";

import { messages } from "@/lib/messages";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { clearPendingLogin, markPendingLogin } from "@/hooks/use-session";
import { login, type LoginState } from "./actions";

const m = messages.auth;

async function loginAndDisarmOnError(
  prev: LoginState | undefined,
  formData: FormData,
): Promise<LoginState | undefined> {
  const result = await login(prev, formData);
  if (result?.error) clearPendingLogin();
  return result;
}

export function LoginForm() {
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
        {pending ? m.loggingIn : m.loginButton}
      </Button>
      <p className="text-center text-xs text-foreground/60">{m.noAccountYet}</p>
    </form>
  );
}
