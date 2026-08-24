"use client";

// Browser-side Supabase client (Client Components only). Reads/writes the
// same cookie-based session @supabase/ssr's server client manages — this is
// what lets SiteHeader show live login state without prop-drilling from a
// Server Component. NEXT_PUBLIC_* vars are inlined at build time (see
// frontend/Dockerfile), so this file has no server-only dependency.
import { createBrowserClient } from "@supabase/ssr";

import { supabasePublicEnv } from "./env";

export function createClient() {
  const { url, anonKey } = supabasePublicEnv();
  return createBrowserClient(url, anonKey);
}
