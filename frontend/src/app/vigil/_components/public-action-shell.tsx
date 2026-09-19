"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConsumeLinkToken } from "@/hooks/use-consume-link-token";
import { confirmVigilPublic, VigilApiError } from "@/lib/vigil/api";
import { VigilConfirmAltcha } from "./vigil-confirm-altcha";

export type PublicVigilAction = "confirm" | "retrieve" | "revoke";

const TITLE_KEYS: Record<PublicVigilAction, string> = {
  confirm: "confirmTitle",
  retrieve: "retrieveTitle",
  revoke: "revokeTitle",
};

export function PublicActionShell({ action }: { action: PublicVigilAction }) {
  const t = useTranslations("vigil.public");
  const token = useConsumeLinkToken();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<"confirmed" | "gone" | "failed" | null>(null);

  async function onConfirm() {
    if (!token || busy) return;
    const altchaInput = document.querySelector('input[name="altcha"]');
    const altcha = altchaInput instanceof HTMLInputElement ? altchaInput.value : "";
    if (!altcha) {
      setResult("failed");
      return;
    }
    setBusy(true);
    try {
      const outcome = await confirmVigilPublic({ token, altcha });
      if (outcome.result === "confirmed" || outcome.result === "already_resolved") {
        setResult("confirmed");
      } else {
        setResult("gone");
      }
    } catch (err) {
      if (err instanceof VigilApiError && (err.status === 410 || err.status === 404)) {
        setResult("gone");
      } else {
        setResult("failed");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t(TITLE_KEYS[action])}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {action !== "confirm" || result === "gone" ? (
          <p>{result === "gone" ? t("gone") : t("unavailable")}</p>
        ) : result === "confirmed" ? (
          <p>{t("confirmed")}</p>
        ) : (
          <>
            <p>{t("confirmHint")}</p>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void onConfirm();
              }}
              className="flex flex-col gap-3"
            >
              <VigilConfirmAltcha />
              <Button type="submit" disabled={busy || !token}>
                {busy ? t("confirming") : t("confirmButton")}
              </Button>
            </form>
            {result === "failed" ? (
              <p className="text-sm text-destructive" role="alert">
                {t("failed")}
              </p>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}
