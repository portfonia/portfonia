import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

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

const PUBLIC_PATH_PREFIXES = ["/login", "/signup", "/api/"];

function isPublicPath(pathname: string): boolean {
  if (pathname === "/") return true;
  return PUBLIC_PATH_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

export async function proxy(request: NextRequest): Promise<NextResponse> {
  let response = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
          response = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options),
          );
        },
      },
    },
  );

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
      // any Set-Cookie the getUser() refresh above already queued on
      // `response` via setAll — losing a just-refreshed session cookie on
      // exactly the requests that prove a session is still active.
      response.cookies.getAll().forEach((cookie) => authedResponse.cookies.set(cookie));
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
