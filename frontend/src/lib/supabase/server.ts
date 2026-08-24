import "server-only";

import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

import { supabasePublicEnv } from "./env";

// Server-side Supabase client (Server Components, Server Actions, Route
// Handlers). `setAll` can throw when called from a Server Component (cookies
// are read-only there) — safe to swallow because proxy.ts refreshes the
// session on every request, so a Server Component read never needs to write
// back a refreshed cookie itself.
export async function createClient() {
  const cookieStore = await cookies();
  const { url, anonKey } = supabasePublicEnv();

  return createServerClient(url, anonKey, {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      // The second argument (`headers`) carries Cache-Control/Expires/Pragma
      // that must land on the HTTP response so a CDN/proxy never caches a
      // Set-Cookie and serves one user's session to another (blacktomb42
      // review, PR #185). It's intentionally unused here, not overlooked:
      // `headers()` from next/headers is read-only in Server Components/
      // Actions (Next's own docs — no API sets outgoing response headers
      // from this context), so there is nothing to apply it to. proxy.ts's
      // own `setAll` is the one place that actually can, and does, apply
      // these headers — it runs on every request, including the one right
      // after any Server Action redirect lands here.
      // eslint-disable-next-line @typescript-eslint/no-unused-vars -- see comment above: intentionally unused, not overlooked.
      setAll(cookiesToSet, _headers) {
        try {
          cookiesToSet.forEach(({ name, value, options }) =>
            cookieStore.set(name, value, options),
          );
        } catch {
          // Called from a Server Component — see comment above.
        }
      },
    },
  });
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
