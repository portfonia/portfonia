"use client";

import { useEffect, useState } from "react";

import { getVigilVaultStatus } from "@/lib/vigil/api";

// Gates the SiteHeader's Vigil nav entry on the caller's own authorized
// backend status (issue #453 scope): the entry only renders once
// GET /vigil/vault actually succeeds for THIS user, never on session status
// alone. A non-owner gets 403, an unconfigured/off deployment gets 503 —
// both must hide the entry silently, not surface an error in the header
// (see lib/vigil/api.ts's getVigilVaultStatus for why this never routes
// through the shared logout() Server Action the way other API reads do).
//
// `enabled` is the caller's session status boolean (only fetch once
// authed) — this hook owns no session logic of its own.
export function useVigilAccess(enabled: boolean): boolean {
  const [available, setAvailable] = useState(false);

  useEffect(() => {
    if (!enabled) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setAvailable(false);
      return;
    }
    let cancelled = false;
    getVigilVaultStatus()
      .then(() => {
        if (!cancelled) setAvailable(true);
      })
      .catch(() => {
        if (!cancelled) setAvailable(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  return available;
}
