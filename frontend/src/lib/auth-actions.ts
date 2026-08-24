"use server";

import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

// Shared logout Server Action — called directly from SiteHeader (a Client
// Component; Next.js compiles imported Server Actions into a fetch for
// client callers automatically) so the logout button works on every route,
// not just inside a page-scoped form.
export async function logout(): Promise<void> {
  const supabase = await createClient();
  await supabase.auth.signOut();
  redirect("/login");
}
