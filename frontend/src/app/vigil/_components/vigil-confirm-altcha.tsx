"use client";

import Script from "next/script";

export function VigilConfirmAltcha() {
  return (
    <>
      <Script src="/altcha.js" type="module" strategy="afterInteractive" />
      <altcha-widget challengeurl="/api/vigil/public/altcha-challenge" name="altcha" />
    </>
  );
}
