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
  "/verify-email",
  "/unsubscribe",
  "/terms",
  "/privacy",
  // The matcher below only excludes image extensions, not .js, so the
  // vendored Altcha widget (frontend/public/altcha.js) still runs through
  // this function — without this entry a logged-out visitor's GET for it
  // 307s to /login before public/ ever serves the file, and the widget on
  // /forgot-password silently never registers (blacktomb42 review, PR #237).
  "/altcha.js",
  "/api/",
];

function isPublicPath(pathname: string): boolean {
  if (pathname === "/") return true;
  return PUBLIC_PATH_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

// Issue #453: exactly these three page routes, plus the future
// /api/vigil/public/* backend prefix (no route exists there yet — #460+ —
// but the exemption is added now so proxy.ts never needs touching again
// when it lands). A recipient following a mailed confirm/retrieve/revoke
// link has no Portfonia session at all, and must not depend on Auth being
// reachable — Design section 5's "public P" scope is a token+nonce, not a
// cookie session. Deliberately NOT a prefix match on "/vigil": /vigil and
// /vigil/setup stay on the normal protected path below.
const EXACT_PUBLIC_VIGIL_PAGES = ["/vigil/confirm", "/vigil/retrieve", "/vigil/revoke"];
const PUBLIC_VIGIL_API_PREFIX = "/api/vigil/public/";

function isVigilAuthExempt(pathname: string): boolean {
  return (
    EXACT_PUBLIC_VIGIL_PAGES.includes(pathname) || pathname.startsWith(PUBLIC_VIGIL_API_PREFIX)
  );
}

// blacktomb42 review, PR #506: next.config.ts sets no `trailingSlash`
// option, so Next defaults to `trailingSlash: false` and does NOT
// normalize the incoming pathname before middleware runs —
// request.nextUrl.pathname carries a trailing slash exactly as the client
// sent it. Every exact-match comparison below (`isVigilAuthExempt`, the
// literal "/vigil" check) needs a normalized value or a mailed link with a
// stray trailing slash would silently fall through to the ordinary
// Supabase/login-redirect path instead of its exemption. Root "/" is left
// alone — there is nothing to strip.
function stripTrailingSlash(pathname: string): string {
  return pathname.length > 1 && pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
}

export async function proxy(request: NextRequest): Promise<NextResponse> {
  const pathname = stripTrailingSlash(request.nextUrl.pathname);

  // Skip the Supabase client/getUser() call entirely for these — not just
  // the redirect-to-login check below. An Auth outage must never turn a
  // stop/confirm link into a 5xx or an indefinite hang.
  if (isVigilAuthExempt(pathname)) {
    return NextResponse.next({ request });
  }

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

  if (!user && !isPublicPath(pathname)) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    url.search = "";
    // Issue #453: the ONLY return destination /login ever accepts besides
    // its own default (/profile) is the literal string "/vigil" — and this
    // is the only place that ever sets it, hardcoded, never echoing
    // anything from the incoming request's own query string. That is what
    // makes "reject any external/protocol-relative return URL" hold by
    // construction rather than by validation: there is no code path that
    // could ever produce another value here.
    if (pathname === "/vigil") {
      url.searchParams.set("next", "/vigil");
    }
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
