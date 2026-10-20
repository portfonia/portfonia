"use client";

import { useEffect, useRef } from "react";

import { SESSION_IDLE_TIMEOUT_MS } from "@/lib/idle-timeout";
import { lastBackendTouch, recordBackendTouch } from "@/lib/session-clock";

export { SESSION_IDLE_TIMEOUT_MS };

const ACTIVITY_EVENTS = [
  "pointerdown",
  "keydown",
  "wheel",
  "touchstart",
  "scroll",
] as const;

const CHECK_INTERVAL_MS = 5_000;

export function useIdleLogout(
  status: "checking" | "guest" | "authed",
  onIdleExpired: (reason?: string) => void,
): void {
  const lastActivity = useRef(0);
  const onIdleExpiredRef = useRef(onIdleExpired);

  useEffect(() => {
    onIdleExpiredRef.current = onIdleExpired;
  }, [onIdleExpired]);

  useEffect(() => {
    if (status !== "authed") return;

    lastActivity.current = Date.now();
    recordBackendTouch(lastActivity.current);
    let lastHeartbeatAttempt = lastActivity.current;
    let inFlight = false;
    let expired = false;
    let active = true;

    const expire = () => {
      if (!active || expired) return;
      expired = true;
      onIdleExpiredRef.current("expired");
    };
    const checkExpiry = () => {
      if (Date.now() >= lastBackendTouch() + SESSION_IDLE_TIMEOUT_MS) expire();
    };
    const stampActivity = () => {
      lastActivity.current = Date.now();
      if (expired || inFlight || lastActivity.current - lastHeartbeatAttempt < 60_000) return;
      lastHeartbeatAttempt = lastActivity.current;
      inFlight = true;
      void fetch("/api/auth/session-status").then(response => {
        if (!active) return;
        if (response.status === 200) recordBackendTouch(Date.now());
        else if (response.status === 401) expire();
      }).catch(() => {
        // A failed heartbeat does not confirm expiry or extend the clock.
      }).finally(() => { inFlight = false; });
    };
    for (const event of ACTIVITY_EVENTS) {
      window.addEventListener(event, stampActivity, { passive: true });
    }
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") checkExpiry();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    const interval = setInterval(checkExpiry, CHECK_INTERVAL_MS);

    return () => {
      active = false;
      clearInterval(interval);
      for (const event of ACTIVITY_EVENTS) {
        window.removeEventListener(event, stampActivity);
      }
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [status]);
}
