"use client";

import Script from "next/script";

// Self-hosted Altcha PoW widget (issue #231) — no external CDN. The script
// tag loads the vendored bundle from public/altcha.js (same-origin); the
// custom element fetches its challenge from /api/auth/altcha-challenge,
// which Next's rewrite (next.config.ts) forwards to the backend's
// GET /auth/altcha-challenge. On solve, the widget writes its own hidden
// <input name="altcha"> into the surrounding <form> (see the README's
// "name" option) — the Server Action reads that field directly, same as
// every other form field here.
export function AltchaWidget() {
  return (
    <>
      <Script src="/altcha.js" type="module" strategy="afterInteractive" />
      <altcha-widget challengeurl="/api/auth/altcha-challenge" name="altcha" />
    </>
  );
}
