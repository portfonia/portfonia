"use client";

import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { createClient } from "@/lib/supabase/browser";

export type SessionState =
  | { status: "checking"; pendingReason: "login" | null }
  | { status: "guest" }
  | { status: "authed"; email: string };

// auth.portfonia.com (the Caddy reverse-proxy to the real Supabase host,
// routing around direct-connectivity issues) normally answers in well under
// a second, but has been observed spiking to ~2.7s under network jitter.
// Bound getUser() so a bad round-trip degrades to a retry within a bounded
// window instead of leaving the UI sitting in `checking` indefinitely
// (issue #214 — read as "stuck," not "slow").
const GET_USER_TIMEOUT_MS = 8_000;

// login()/logout() are Server Actions whose redirect() lands on a page
// where SiteHeader never remounts (shared root layout) — see the mount-time
// re-verification note below. Module-level signals let the component that
// triggered the action (gone by the time the next page mounts) tell the
// next useSession instance what's going on, since there is no other
// channel between them:
//
// - markPendingLogin(): the login form's onSubmit calls this right before
//   the Server Action runs. A real 2026-08-26 user report caught the
//   regression this replaces — a "rapid navigation" grace window (PR #215
//   review) that collapsed the pathname change following a successful
//   login into a no-op, so the menu kept showing the pre-login guest state
//   until an unrelated focus/visibility event happened to fire. There is no
//   grace window anymore: every pathname change re-verifies, unconditionally.
//   This signal exists purely so the `checking` window that a real
//   getUser() round-trip still takes can say "logging in..." instead of
//   rendering nothing, since the reason IS known at this specific moment
//   (a future business-logic hook for the post-login transition can key off
//   the same signal).
// - markOptimisticLogout(): the Log out button calls this so the menu
//   flips to guest immediately, without waiting on a verified getUser()
//   round-trip over the auth.portfonia.com proxy. The Server Action can
//   still fail (network error, signOut() rejection) — the click handler
//   must catch that and call revalidateSession() to drop the optimistic
//   guest state, otherwise the UI claims logged-out while the cookie
//   session is intact (issue #217 review).
// - clearPendingLogin(): login/signup forms call this when the Server
//   Action returns an error instead of redirecting, so a later ordinary
//   navigation does not consume a stale "Logging in..." signal.
// - revalidateSession(): force every mounted useSession to run verify()
//   now, discarding in-flight results the same way SIGNED_IN does.
let pendingLoginSignal = false;

export function markPendingLogin() {
  pendingLoginSignal = true;
}

export function clearPendingLogin() {
  pendingLoginSignal = false;
}

export function __resetSessionSignalsForTests() {
  pendingLoginSignal = false;
}

const optimisticLogoutListeners = new Set<() => void>();
const revalidateListeners = new Set<() => void>();

export function markOptimisticLogout() {
  optimisticLogoutListeners.forEach((fn) => fn());
}

export function revalidateSession() {
  revalidateListeners.forEach((fn) => fn());
}

type GetUserResult = Awaited<ReturnType<ReturnType<typeof createClient>["auth"]["getUser"]>>;

// Distinguishes "the round-trip took too long" from any other rejection
// (DNS refusal, connection reset, ...) — only the former is worth a retry.
// A dead endpoint retried with zero backoff just fails again immediately,
// doubling the fail-closed latency for no benefit (PR #215 review).
class GetUserTimeoutError extends Error {}

// supabase-js's getUser(jwt?: string) has no AbortSignal parameter, so a
// timed-out attempt's underlying request cannot be cancelled here without
// wrapping createBrowserClient's own `fetch` option — a materially larger
// change than this timeout/retry budget calls for. The orphaned request
// still resolves eventually; the generation/cancelled guards in verify()
// already discard it correctly, so this is a wasted round-trip, not a
// correctness gap.
function getUserWithTimeout(
  supabase: ReturnType<typeof createClient>,
): Promise<GetUserResult> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new GetUserTimeoutError("getUser() timed out")),
      GET_USER_TIMEOUT_MS,
    );
    supabase.auth.getUser().then(
      (result) => {
        clearTimeout(timer);
        resolve(result);
      },
      (err: unknown) => {
        clearTimeout(timer);
        reject(err instanceof Error ? err : new Error(String(err)));
      },
    );
  });
}

// One retry, no extra delay, and ONLY for a timeout — an immediate network
// error (refused connection, DNS failure) is not a proxy hiccup and will
// just fail the same way again.
async function verifiedGetUser(
  supabase: ReturnType<typeof createClient>,
): Promise<GetUserResult> {
  try {
    return await getUserWithTimeout(supabase);
  } catch (err) {
    if (!(err instanceof GetUserTimeoutError)) throw err;
    return await getUserWithTimeout(supabase);
  }
}

// Display truth comes ONLY from a verified getUser() call. The
// INITIAL_SESSION auth event carries the locally persisted session WITHOUT
// server verification — trusting it is what let a revoked/expired session
// keep rendering its email after a refresh (issue #207 D1).
//
// Auth-event resolutions are guarded by a generation counter, not a sticky
// flag: SIGNED_OUT bumps the generation so an in-flight getUser() that
// resolves afterwards is discarded (it would otherwise flip the menu back to
// authed mid-logout), while SIGNED_IN bumps it too and triggers a fresh
// verify() — logout-then-login in the same tab must recover without a full
// reload, and the SIGNED_IN payload itself is still not trusted (same D1
// hole as INITIAL_SESSION).
export function useSession(): SessionState {
  const [state, setState] = useState<SessionState>({
    status: "checking",
    pendingReason: null,
  });
  // login()/logout() are Server Actions that redirect() — SiteHeader lives
  // in the shared root layout and never remounts across that navigation, so
  // its own useEffect(..., []) would never re-run and the menu would only
  // ever catch up via the focus/visibility fallback (issue #214: read as
  // "topbar refresh is slow" or "stuck," since that fallback has no bound on
  // when the user will actually trigger it). usePathname() DOES change on
  // every such redirect even though the component doesn't remount, so it's
  // used here purely as a "something navigated, re-verify" signal — not
  // because the session itself is scoped to a route.
  const pathname = usePathname();

  useEffect(() => {
    const supabase = createClient();
    let cancelled = false;
    let generation = 0;
    let inFlight: Promise<void> | null = null;

    // Consumed once per mount, not per verify() call within it — a
    // focus/visibility re-check later in this same mounted instance is not
    // "logging in" again, only the very first verify() after a login
    // redirect is. Synchronous setState here is intentional (same pattern
    // as locale-provider.tsx): this effect exists specifically to
    // synchronize with the pendingLoginSignal module state left behind by a
    // component that's already gone by the time this one mounts (the login
    // form, on the previous page) — there's no other lifecycle point to
    // read and consume it from.
    if (pendingLoginSignal) {
      pendingLoginSignal = false;
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setState({ status: "checking", pendingReason: "login" });
    }

    const onOptimisticLogout = () => {
      // Discard whatever verify() is in flight the same way a real
      // SIGNED_OUT event does — its eventual result (even "authed") must
      // never override what the user just told the app to do.
      generation += 1;
      inFlight = null;
      setState({ status: "guest" });
    };
    optimisticLogoutListeners.add(onOptimisticLogout);

    const verify = () => {
      // Each verify() captures the current generation; a resolution from a
      // superseded generation (a SIGNED_OUT/SIGNED_IN arrived meanwhile) is
      // stale and ignored. One shared in-flight promise per generation:
      // focus and visibilitychange fire together on tab return — two
      // listeners, one network call.
      if (inFlight) return;
      const myGeneration = generation;
      inFlight = verifiedGetUser(supabase)
        .then(({ data }) => {
          if (cancelled || myGeneration !== generation) return;
          setState(
            data.user
              ? { status: "authed", email: data.user.email ?? "" }
              : { status: "guest" },
          );
        })
        .catch(() => {
          // Fail closed: an unverifiable session must never leave a stale
          // identity on screen. Worst case degrades to showing Log in (D2).
          if (cancelled || myGeneration !== generation) return;
          setState({ status: "guest" });
          console.warn(
            "[i] useSession: getUser() failed; rendering logged-out state",
          );
        })
        .finally(() => {
          if (myGeneration === generation) inFlight = null;
        });
    };

    const onForcedRevalidate = () => {
      // Drop the optimistic guest (or any other in-flight generation) and
      // ask the server again. Used when logout() rejects after
      // markOptimisticLogout() already flipped the menu.
      generation += 1;
      inFlight = null;
      verify();
    };
    revalidateListeners.add(onForcedRevalidate);

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
    } = supabase.auth.onAuthStateChange((event) => {
      if (event === "SIGNED_OUT") {
        generation += 1; // discard any verify() launched before logout
        inFlight = null; // allow a later SIGNED_IN verify to start immediately
        setState({ status: "guest" });
        return;
      }
      if (event === "SIGNED_IN") {
        // Do NOT trust this event's session.user (local cache, D1). Bump the
        // generation so any pre-login verify() is discarded, then re-verify.
        generation += 1;
        inFlight = null;
        verify();
        return;
      }
      if (event === "USER_UPDATED") {
        verify();
        return;
      }
      // INITIAL_SESSION / TOKEN_REFRESHED are deliberately ignored: they
      // reflect local cache, not server-verified truth. Revalidation runs
      // via verify() on mount and on focus/visibility instead.
    });

    return () => {
      cancelled = true;
      optimisticLogoutListeners.delete(onOptimisticLogout);
      revalidateListeners.delete(onForcedRevalidate);
      subscription.unsubscribe();
      document.removeEventListener("visibilitychange", revalidate);
      window.removeEventListener("focus", revalidate);
    };
  }, [pathname]);

  return state;
}
