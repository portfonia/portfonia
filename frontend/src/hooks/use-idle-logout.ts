"use client";

import { useEffect, useRef } from "react";

// Client-side idle auto-logout (issue #207 R6): after SESSION_IDLE_TIMEOUT_MS
// with no user activity in the tab, invoke the callback (the shared logout
// Server Action). Convenience/privacy measure on top of the cookie session —
// it cannot revoke anything server-side; real lifetime enforcement belongs to
// the Auth provider config.
export const SESSION_IDLE_TIMEOUT_MS = 15 * 60_000;

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
  onIdleExpired: () => void,
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

    const interval = setInterval(() => {
      if (expiredRef.current) return;
      if (Date.now() - lastActivity.current >= SESSION_IDLE_TIMEOUT_MS) {
        expiredRef.current = true;
        onIdleExpiredRef.current();
      }
    }, CHECK_INTERVAL_MS);

    return () => {
      clearInterval(interval);
      for (const event of ACTIVITY_EVENTS) {
        window.removeEventListener(event, stampActivity);
      }
    };
  }, [status]);
}
