"use client";

import Script from "next/script";

// Self-hosted Altcha PoW widget (issue #260), mirrors
// forgot-password/_components/altcha-widget.tsx exactly — same vendored
// bundle, same "widget writes its own hidden <input> into the surrounding
// <form>" mechanism, pointed at the email-verification confirm flow's own
// challenge endpoint instead.
export function AltchaWidget() {
  return (
    <>
      <Script src="/altcha.js" type="module" strategy="afterInteractive" />
      <altcha-widget challengeurl="/api/email-verifications/altcha-challenge" name="altcha" />
    </>
  );
}
