"use client";

import Script from "next/script";

// Self-hosted Altcha PoW widget (issue #393), mirrors
// forgot-password/_components/altcha-widget.tsx — same vendored bundle,
// same hidden <input name="altcha"> write into the surrounding form.
// Challenge URL is the authed GET /me/change-password/altcha-challenge
// (proxy.ts injects Bearer on /api/* before the rewrite).
export function AltchaWidget() {
  return (
    <>
      <Script src="/altcha.js" type="module" strategy="afterInteractive" />
      <altcha-widget challengeurl="/api/me/change-password/altcha-challenge" name="altcha" />
    </>
  );
}
