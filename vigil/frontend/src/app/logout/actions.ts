"use server";

import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

export async function logout(): Promise<void> {
  const supabase = await createClient();
  await supabase.auth.signOut();
  redirect("/");
}
