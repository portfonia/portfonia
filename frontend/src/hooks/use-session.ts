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
    // Set the moment a SIGNED_OUT event is observed: any verify() still in
    // flight must not flip state back to authed after it resolves (the
    // header survives client-side redirects, so a stale authed menu would
    // otherwise persist until the next revalidation).
    let signedOutObserved = false;
    let inFlight: Promise<void> | null = null;

    const applyVerifiedUser = (user: { email?: string } | null | undefined) => {
      if (cancelled || signedOutObserved) return;
      setState(
        user
          ? { status: "authed", email: user.email ?? "" }
          : { status: "guest" },
      );
    };

    const verify = () => {
      // One shared in-flight promise per hook instance: focus and
      // visibilitychange fire together on tab return — two listeners, one
      // network call.
      if (inFlight) return;
      inFlight = supabase.auth
        .getUser()
        .then(({ data }) => {
          applyVerifiedUser(data.user);
        })
        .catch(() => {
          // Fail closed: an unverifiable session must never leave a stale
          // identity on screen. Worst case degrades to showing Log in (D2).
          if (!cancelled && !signedOutObserved) {
            setState({ status: "guest" });
            console.warn(
              "[i] useSession: getUser() failed; rendering logged-out state",
            );
          }
        })
        .finally(() => {
          inFlight = null;
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
        signedOutObserved = true;
        setState({ status: "guest" });
        return;
      }
      if (event === "USER_UPDATED" && session?.user) {
        signedOutObserved = false;
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
