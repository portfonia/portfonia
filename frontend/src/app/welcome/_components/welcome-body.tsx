"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import type { Me } from "@/lib/api";

// sessionStorage-only dedupe (Ring 1-Onboarding.md §2.4) — not an entry
// condition. Reachable from questionnaire onboarding Save or holdings
// onboarding save; a later direct visit this session bounces to "/".
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
    try {
      sessionStorage.setItem(WELCOMED_KEY, "1");
    } catch {
      // Storage unavailable (private mode, blocked site data) — degrade to
      // always showing the page rather than throwing.
    }
    // One-time client-only reveal, same pattern (and same justification) as
    // locale-provider.tsx's restore effect: sessionStorage can't be read
    // during SSR/first paint without a hydration mismatch.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setReady(true);
  }, [router]);

  if (!ready) return null;

  if (hadLoadError || !me) {
    return (
      <p className="text-sm text-destructive" role="alert">
        {t("errorLoadFailed")}
      </p>
    );
  }

  const deliveryEmail = me.delivery_email ?? me.email;

  return (
    <div className="flex flex-col gap-4">
      <h1 className="font-heading text-2xl font-semibold">
        {t("greeting", { email: me.email })}
      </h1>
      <p className="text-sm text-foreground/80">
        {me.has_holdings
          ? t("withHoldings", { deliveryEmail })
          : t("withoutHoldings", { deliveryEmail })}
      </p>
      <p className="text-sm text-foreground/80">{t("cadence")}</p>
    </div>
  );
}
