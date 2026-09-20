"use client";

import { useEffect, useState } from "react";

// Design section 6 (#450): the public Vigil action links carry their token
// in the URL FRAGMENT (`/vigil/confirm#<token>`), which the browser never
// sends to the server on the initial request — read it into memory once,
// then strip it via replaceState so it never sits in the visible URL bar,
// browser history, or an analytics/referrer read. No local/sessionStorage.
//
// Reads in a mount effect, not a useState lazy initializer, for the same
// hydration-mismatch reason as locale-provider.tsx's restore effect (see
// docs/mechanisms/frontend-chrome.md's welcome-page section): the server
// render has no `window`, so both server and client must render `null` on
// first paint, with the real value applied only after hydration.
//
// The public action sends the in-memory token only after the visitor solves
// Altcha and explicitly submits the confirmation form.
export function useConsumeLinkToken(): string | null {
  const [token, setToken] = useState<string | null>(null);

  useEffect(() => {
    const raw = window.location.hash.slice(1);
    if (!raw) return;
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setToken(raw);
  }, []);

  return token;
}
