"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import type { Me } from "@/lib/api";

// sessionStorage-only dedupe (Ring 1-Onboarding.md §2.4) — not an entry
// condition. Reachable from holdings onboarding save or its skip
// (questionnaire onboarding save now routes to /holdings?onboarding=1,
// issue #280 §9.1); a later direct visit this session bounces to "/".
const WELCOMED_KEY = "portfonia.welcomed";

export function WelcomeBody({ me, hadLoadError }: { me: Me | null; hadLoadError: boolean }) {
  const t = useTranslations("welcome");
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let alreadyWelcomed = false;
    try {
      alreadyWelcomed = sessionStorage.getItem(WELCOMED_KEY) === "1";
    } catch {
      alreadyWelcomed = false;
    }
    if (alreadyWelcomed) {
      router.replace("/");
      return;
    }
    // Only burn the one-shot on an actual successful load (blacktomb42
    // review, PR #230) — marking it welcomed on a failed GET /me would
    // bounce a legitimate retry straight to "/" once the fetch recovers,
    // with no second chance to ever see this page.
    if (!hadLoadError && me) {
      try {
        sessionStorage.setItem(WELCOMED_KEY, "1");
      } catch {
        // Storage unavailable (private mode, blocked site data) — degrade
        // to always showing the page rather than throwing.
      }
    }
    // One-time client-only reveal, same pattern (and same justification) as
    // locale-provider.tsx's restore effect: sessionStorage can't be read
    // during SSR/first paint without a hydration mismatch.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setReady(true);
  }, [router, hadLoadError, me]);

  if (!ready) return null;

  if (hadLoadError || !me) {
    return (
      <p className="text-sm text-destructive" role="alert">
        {t("errorLoadFailed")}
      </p>
    );
  }

  const deliveryEmail = me.delivery_email ?? me.email;
  // Issue #280 §9.2 / #290: unverified is derived per scope, same rule as
  // the Profile page (issue #269 §6) — a set delivery_email is checked against
  // its own timestamp; the account-email fallback against the account one.
  const receivingEmailUnverified =
    (me.delivery_email != null && me.delivery_email_verified_at == null) ||
    (me.delivery_email == null && me.email_verified_at == null);

  return (
    <div className="flex flex-col gap-4">
      <h1 className="font-heading text-2xl font-semibold">
        {t("greeting", { email: me.email })}
      </h1>
      <p className="text-sm text-foreground/80">
        {me.has_holdings ? t("withHoldings") : t("withoutHoldings")}
      </p>
      <p className="text-sm text-foreground/80">
        {receivingEmailUnverified
          ? t("deliveryUnverified", { deliveryEmail })
          : t("deliveryVerified", { deliveryEmail })}
      </p>
      <p className="text-sm text-foreground/80">{t("cadence")}</p>
    </div>
  );
}
