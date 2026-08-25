"use client";

import { useEffect, useRef } from "react";

import { SESSION_IDLE_TIMEOUT_MS } from "@/lib/idle-timeout";

// Client-side idle auto-logout (issue #207 R6): after SESSION_IDLE_TIMEOUT_MS
// with no user activity IN THE TAB, invoke the callback (the shared logout
// Server Action). Convenience/privacy measure on top of the cookie session —
// it cannot revoke anything server-side; real lifetime enforcement belongs to
// the Auth provider config. Background time does NOT count: the interval
// skips while the tab is hidden and the clock restarts when it becomes
// visible again, so working in another window never triggers a surprise
// logout.
export { SESSION_IDLE_TIMEOUT_MS };

const ACTIVITY_EVENTS = [
  "pointerdown",
  "keydown",
  "wheel",
  "touchstart",
  "scroll",
] as const;

const CHECK_INTERVAL_MS = 30_000;

export function useIdleLogout(
  status: "checking" | "guest" | "authed",
  onIdleExpired: (reason?: string) => void,
): void {
  // Refs, not state: activity stamps and expiry must never re-render the bar.
  const lastActivity = useRef(0);
  const expiredRef = useRef(false);
  const onIdleExpiredRef = useRef(onIdleExpired);

  useEffect(() => {
    onIdleExpiredRef.current = onIdleExpired;
  }, [onIdleExpired]);

  useEffect(() => {
    if (status !== "authed") return;

    lastActivity.current = Date.now();
    expiredRef.current = false;

    const stampActivity = () => {
      lastActivity.current = Date.now();
    };
    for (const event of ACTIVITY_EVENTS) {
      window.addEventListener(event, stampActivity, { passive: true });
    }

    // Returning to the tab restarts the idle clock — hidden stretches are
    // neither activity nor idleness.
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") stampActivity();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);

    const interval = setInterval(() => {
      // Skip entirely while hidden: a parked tab must not accumulate
      // idle time toward logout.
      if (expiredRef.current || document.hidden) return;
      if (Date.now() - lastActivity.current >= SESSION_IDLE_TIMEOUT_MS) {
        expiredRef.current = true;
        onIdleExpiredRef.current("expired");
      }
    }, CHECK_INTERVAL_MS);

    return () => {
      clearInterval(interval);
      for (const event of ACTIVITY_EVENTS) {
        window.removeEventListener(event, stampActivity);
      }
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [status]);
}
