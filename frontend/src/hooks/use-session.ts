"use client";

import { useEffect, useState } from "react";

import { createClient } from "@/lib/supabase/browser";

export type SessionState =
  | { status: "checking" }
  | { status: "guest" }
  | { status: "authed"; email: string };

// Display truth comes ONLY from a verified getUser() call. The
// INITIAL_SESSION auth event carries the locally persisted session WITHOUT
// server verification — trusting it is what let a revoked/expired session
// keep rendering its email after a refresh (issue #207 D1).
export function useSession(): SessionState {
  const [state, setState] = useState<SessionState>({ status: "checking" });

  useEffect(() => {
    const supabase = createClient();
    let cancelled = false;

    const verify = () => {
      supabase.auth
        .getUser()
        .then(({ data }) => {
          if (!cancelled) {
            setState(
              data.user
                ? { status: "authed", email: data.user.email ?? "" }
                : { status: "guest" },
            );
          }
        })
        .catch(() => {
          // Fail closed: an unverifiable session must never leave a stale
          // identity on screen. Worst case degrades to showing Log in (D2).
          if (!cancelled) {
            setState({ status: "guest" });
            console.warn(
              "[i] useSession: getUser() failed; rendering logged-out state",
            );
          }
        });
    };

    verify();

    // A parked tab performs no navigation, so nothing else re-checks a
    // session that was revoked while the tab sat idle.
    const revalidate = () => {
      if (document.visibilityState === "visible") verify();
    };
    document.addEventListener("visibilitychange", revalidate);
    window.addEventListener("focus", revalidate);

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((event, session) => {
      if (event === "SIGNED_OUT") {
        setState({ status: "guest" });
        return;
      }
      if (event === "USER_UPDATED" && session?.user) {
        setState({ status: "authed", email: session.user.email ?? "" });
      }
      // INITIAL_SESSION / TOKEN_REFRESHED are deliberately ignored: they
      // reflect local cache, not server-verified truth. Revalidation runs
      // via verify() on mount and on focus/visibility instead.
    });

    return () => {
      cancelled = true;
      subscription.unsubscribe();
      document.removeEventListener("visibilitychange", revalidate);
      window.removeEventListener("focus", revalidate);
    };
  }, []);

  return state;
}
