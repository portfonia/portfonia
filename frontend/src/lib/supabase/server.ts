import "server-only";

import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

// Server-side Supabase client (Server Components, Server Actions, Route
// Handlers). `setAll` can throw when called from a Server Component (cookies
// are read-only there) — safe to swallow because proxy.ts refreshes the
// session on every request, so a Server Component read never needs to write
// back a refreshed cookie itself.
export async function createClient() {
  const cookieStore = await cookies();

  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return cookieStore.getAll();
        },
        setAll(cookiesToSet) {
          try {
            cookiesToSet.forEach(({ name, value, options }) =>
              cookieStore.set(name, value, options),
            );
          } catch {
            // Called from a Server Component — see comment above.
          }
        },
      },
    },
  );
}

// Backend calls (rewrite-independent paths: the upload Route Handler, SSR
// direct reads) need the raw Bearer token, not a Supabase client — the
// FastAPI backend's `current_principal` only understands
// `Authorization: Bearer <access_token>` (Ring 1-B design doc §6.5), it has
// no notion of a Supabase cookie session. Returns null rather than throwing
// so callers can each decide their own 401/redirect shape.
export async function currentAccessToken(): Promise<string | null> {
  const supabase = await createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();
  return session?.access_token ?? null;
}
