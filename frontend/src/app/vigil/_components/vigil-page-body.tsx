"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { VigilVaultLoadResult } from "@/lib/vigil/server";
import { deriveVigilDisplayState } from "../_lib/vault-display-state";

// Split out from page.tsx (same reasoning as profile-page-body.tsx and
// questionnaire-page-body.tsx): whether the page renders at all depends on
// translated content, so it isn't worth carving that back into the Server
// Component just to keep page.tsx heading-only. Read-only display for this
// checkpoint (issue #453) — no arm/disarm/revoke controls here yet, those
// belong to #458-462 once the underlying backend actions exist.
export function VigilPageBody({ result }: { result: VigilVaultLoadResult }) {
  const t = useTranslations("vigil");
  const state = deriveVigilDisplayState(result);
  const isHold = state === "hold";

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p>{t(`states.${state}`)}</p>
        {isHold && result.status === "ok" && result.vault.hold_reason && (
          <p className="text-sm text-foreground/60">{result.vault.hold_reason}</p>
        )}
        {state === "setupRequired" && (
          <Button render={<Link href="/vigil/setup" />}>{t("setupCta")}</Button>
        )}
      </CardContent>
    </Card>
  );
}
