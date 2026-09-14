import "server-only";

import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

import { supabasePublicEnv } from "./env";

export async function createClient() {
  const cookieStore = await cookies();
  const { url, anonKey } = supabasePublicEnv();

  return createServerClient(url, anonKey, {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      // eslint-disable-next-line @typescript-eslint/no-unused-vars -- setAll headers are applied in proxy.ts
      setAll(cookiesToSet, _headers) {
        try {
          cookiesToSet.forEach(({ name, value, options }) =>
            cookieStore.set(name, value, options),
          );
        } catch {
          // Called from a Server Component — proxy.ts refreshes the session.
        }
      },
    },
  });
}

export async function currentAccessToken(): Promise<string | null> {
  const supabase = await createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();
  return session?.access_token ?? null;
}

export async function bearerAuthHeaders(): Promise<HeadersInit> {
  const token = await currentAccessToken();
  return token ? { authorization: `Bearer ${token}` } : {};
}
