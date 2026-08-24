"use server";

import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

export interface LoginState {
  error: string | null;
}

export async function login(
  _prevState: LoginState | undefined,
  formData: FormData,
): Promise<LoginState | undefined> {
  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");

  if (!email || !password) {
    return { error: "Enter your email and password." };
  }

  const supabase = await createClient();
  const { error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) {
    // Never surface the Auth provider's own error text (e.g. "invalid_grant")
    // to the client — it can distinguish "no such account" from "wrong
    // password" for an attacker probing emails.
    return { error: "Invalid email or password." };
  }

  redirect("/holdings");
}
