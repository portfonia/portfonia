// Shared, explicit read of the two Supabase public env vars — used by
// proxy.ts, browser.ts, and server.ts alike so the "missing value" error
// exists in exactly one place. This repo's TS quality gate forbids
// non-null assertions (`!`); createServerClient/createBrowserClient would
// already throw on an empty string, but only after producing a confusing
// library-internal error — this fails with a clear message at the actual
// call site instead (blacktomb42 review, PR #185).
export function supabasePublicEnv(): { url: string; anonKey: string } {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL?.trim();
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY?.trim();
  if (!url) {
    throw new Error("NEXT_PUBLIC_SUPABASE_URL must be set");
  }
  if (!anonKey) {
    throw new Error("NEXT_PUBLIC_SUPABASE_ANON_KEY must be set");
  }
  return { url, anonKey };
}
