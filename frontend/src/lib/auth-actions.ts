"use server";

import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

// Shared logout Server Action — called directly from SiteHeader (a Client
// Component; Next.js compiles imported Server Actions into a fetch for
// client callers automatically) so the logout button works on every route,
// not just inside a page-scoped form. `reason` is appended to the redirect
// so /login can explain WHY the logout happened (e.g. the idle auto-logout
// passes "expired"; manual logout passes nothing).
export async function logout(reason?: string): Promise<void> {
  const supabase = await createClient();
  await supabase.auth.signOut();
  redirect(reason ? `/login?reason=${encodeURIComponent(reason)}` : "/login");
}
