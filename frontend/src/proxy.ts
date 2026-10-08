import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { PROTECTED_PATH_PREFIXES, ROUTE_LOCALE_HEADER, isSeoPath, splitLocalePrefix } from "@/lib/seo";

import { supabasePublicEnv } from "@/lib/supabase/env";

// Next.js 16 renamed the `middleware.ts` file convention to `proxy.ts` (the
// function itself is unchanged) — see frontend/AGENTS.md's warning to check
// node_modules/next/dist/docs before assuming a training-data API still
// applies. Do not rename this back to middleware.ts.
//
// Two independent jobs, both required by Ring 1-B design doc §7.3:
//
// 1. Optimistic route protection: redirect an unauthenticated request to a
//    protected page to /login. This is a UX convenience only — the FastAPI
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

function isProtectedPath(pathname: string): boolean {
  let path: string;
  try {
    // Decode once, collapse separators, and preserve decoded dot segments.
    path = decodeURIComponent(pathname).replace(/\/+/g, "/");
  } catch {
    return false;
  }
  return PROTECTED_PATH_PREFIXES.some((prefix) => path === prefix || path.startsWith(`${prefix}/`));
}

// blacktomb42 review, PR #506: next.config.ts sets no `trailingSlash`
// option, so Next defaults to `trailingSlash: false` and does NOT
// normalize the incoming pathname before middleware runs —
// request.nextUrl.pathname carries a trailing slash exactly as the client
// sent it. Every exact-match comparison below needs a normalized value or a
// stray trailing slash would silently change behavior. Root "/" is left
// alone — there is nothing to strip.
function stripTrailingSlash(pathname: string): string {
  return pathname.length > 1 && pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
}

export async function proxy(request: NextRequest): Promise<NextResponse> {
  request.headers.delete(ROUTE_LOCALE_HEADER);
  const pathname = stripTrailingSlash(request.nextUrl.pathname);

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

  // Construct forwarded headers only after getUser has refreshed request cookies.
  const headers = new Headers(request.headers);
  headers.delete(ROUTE_LOCALE_HEADER);
  const refreshedCookies = response.cookies.getAll();
  response = NextResponse.next({ request: { headers } });
  refreshedCookies.forEach((cookie) => response.cookies.set(cookie));
  Object.entries(refreshHeaders).forEach(([key, value]) => response.headers.set(key, value));

  const { locale: routeLocale, path } = splitLocalePrefix(pathname);
  if (routeLocale) {
    headers.set(ROUTE_LOCALE_HEADER, routeLocale);
    const url = request.nextUrl.clone();
    url.pathname = path;
    const localizedResponse = isSeoPath(path)
      ? NextResponse.rewrite(url, { request: { headers } })
      : NextResponse.next({ request: { headers } });
    response.cookies.getAll().forEach((cookie) => localizedResponse.cookies.set(cookie));
    Object.entries(refreshHeaders).forEach(([key, value]) => localizedResponse.headers.set(key, value));
    return localizedResponse;
  }

  if (!user && isProtectedPath(pathname)) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    url.search = "";
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
