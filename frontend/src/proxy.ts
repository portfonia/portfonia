import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { supabasePublicEnv } from "@/lib/supabase/env";

// Next.js 16 renamed the `middleware.ts` file convention to `proxy.ts` (the
// function itself is unchanged) — see frontend/AGENTS.md's warning to check
// node_modules/next/dist/docs before assuming a training-data API still
// applies. Do not rename this back to middleware.ts.
//
// Two independent jobs, both required by Ring 1-B design doc §7.3:
//
// 1. Optimistic route protection: redirect an unauthenticated request to a
//    non-public page to /login. This is a UX convenience only — the FastAPI
//    backend's `current_principal` is the real, non-bypassable boundary
//    (Next's own guidance: proxy must never be the only line of defense).
// 2. Bearer-token injection for the one path that has no server code of its
//    own to attach it: a browser `fetch("/api/...")` from a Client
//    Component goes straight through next.config.ts's declarative rewrite
//    to the backend with no Node.js code in between. The backend only
//    understands `Authorization: Bearer <access_token>` (Ring 1-B §6.5) —
//    it has no notion of a Supabase cookie session — so this is the only
//    place that can turn "there is a valid session cookie" into that
//    header before the rewrite fires (Proxy runs before rewrites in Next's
//    execution order). The upload Route Handler and the SSR direct path
//    each derive this token themselves instead of trusting header
//    propagation here — see api/holdings/upload/route.ts and
//    lib/server-api.ts.

const PUBLIC_PATH_PREFIXES = [
  "/login",
  "/signup",
  "/forgot-password",
  "/reset-password",
  "/api/",
];

function isPublicPath(pathname: string): boolean {
  if (pathname === "/") return true;
  return PUBLIC_PATH_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

export async function proxy(request: NextRequest): Promise<NextResponse> {
  let response = NextResponse.next({ request });
  const { url, anonKey } = supabasePublicEnv();

  // Captured live from setAll's own second argument rather than a
  // hardcoded header-name list: @supabase/ssr is pinned to ^0.12.4, so a
  // future 0.12.x that adds another safety header would silently fall
  // through a hardcoded list (and a test asserting against the same
  // hardcoded list wouldn't catch it either) — the library stays the one
  // source of truth for what must accompany its own Set-Cookie (PR #185
  // round-3 review).
  let refreshHeaders: Record<string, string> = {};

  const supabase = createServerClient(url, anonKey, {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      // `headers` carries Cache-Control/Expires/Pragma that @supabase/ssr
      // requires on any response that sets auth cookies, so a CDN/reverse
      // proxy never caches a Set-Cookie and serves one user's session to
      // another (the library's own SetAllCookies type doc — verified
      // against node_modules/@supabase/ssr, not assumed). Unlike
      // lib/supabase/server.ts, this context genuinely can apply them.
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

  // getUser() re-verifies against the Auth provider (unlike getSession(),
  // which only reads the local JWT) — this is what actually refreshes an
  // expired access token and rewrites the session cookie via setAll above.
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const pathname = request.nextUrl.pathname;

  if (!user && !isPublicPath(pathname)) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }

  if (user && pathname.startsWith("/api/")) {
    const {
      data: { session },
    } = await supabase.auth.getSession();
    if (session) {
      const headers = new Headers(request.headers);
      headers.set("authorization", `Bearer ${session.access_token}`);
      const authedResponse = NextResponse.next({ request: { headers } });
      // Constructing a fresh NextResponse here (required to carry the
      // mutated request headers upstream) would otherwise silently drop
      // anything the getUser() refresh above already queued on `response`
      // via setAll — both the Set-Cookie itself and, same class of bug,
      // the cache-prevention headers alongside it (blacktomb42 review,
      // PR #185) — losing either on exactly the requests that prove a
      // session is still active.
      response.cookies.getAll().forEach((cookie) => authedResponse.cookies.set(cookie));
      // Only what setAll actually handed us — never a blanket copy of
      // `response.headers`, which also carries Next's own bookkeeping
      // headers (`x-middleware-override-headers` etc.) that a blind copy
      // would clobber (PR #185 round-2 review — a real bug from an
      // earlier version of this exact line).
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
