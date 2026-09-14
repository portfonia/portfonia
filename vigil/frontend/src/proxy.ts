import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { supabasePublicEnv } from "@/lib/supabase/env";

// Next.js 16 renamed middleware.ts to proxy.ts. Do not rename this back.
//
// Management chrome is decided by the page after GET /vault, not by a
// blanket unauthenticated redirect. Future public confirm/retrieve routes
// (#461) must keep loading without a Vigil host session.
//
// This proxy only refreshes the host-scoped session and injects
// Authorization: Bearer for the /api rewrite to vigil-backend.

export async function proxy(request: NextRequest): Promise<NextResponse> {
  let response = NextResponse.next({ request });
  const { url, anonKey } = supabasePublicEnv();
  let refreshHeaders: Record<string, string> = {};

  const supabase = createServerClient(url, anonKey, {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet, headers) {
        cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
        response = NextResponse.next({ request });
        cookiesToSet.forEach(({ name, value, options }) =>
          response.cookies.set(name, value, options),
        );
        refreshHeaders = headers;
        Object.entries(headers).forEach(([key, value]) => response.headers.set(key, value));
      },
    },
  });

  const {
    data: { user },
  } = await supabase.auth.getUser();

  const pathname = request.nextUrl.pathname;

  if (user && pathname.startsWith("/api/")) {
    const {
      data: { session },
    } = await supabase.auth.getSession();
    if (session) {
      const headers = new Headers(request.headers);
      headers.set("authorization", `Bearer ${session.access_token}`);
      const authedResponse = NextResponse.next({ request: { headers } });
      response.cookies.getAll().forEach((cookie) => authedResponse.cookies.set(cookie));
      Object.entries(refreshHeaders).forEach(([key, value]) =>
        authedResponse.headers.set(key, value),
      );
      response = authedResponse;
    }
  }

  return response;
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
