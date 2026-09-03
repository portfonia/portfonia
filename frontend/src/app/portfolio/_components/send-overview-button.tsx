"use client";

import { useState, useTransition } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { sendPortfolioOverview } from "@/lib/api";

// issue #202: explicit, user-clicked "Send holdings overview" — NOT a
// formal report. The 15-minute cooldown is enforced server-side
// (check_portfolio_overview_cooldown); this component only renders whatever
// the endpoint reports back, it does not run its own client-side timer.
export function SendOverviewButton({ baseCurrency }: { baseCurrency: string }) {
  const t = useTranslations("portfolio");
  const [isPending, startTransition] = useTransition();
  const [status, setStatus] = useState<
    { kind: "idle" } | { kind: "sent" } | { kind: "cooldown"; minutes: number } | { kind: "error" }
  >({ kind: "idle" });

  const handleClick = () => {
    startTransition(async () => {
      try {
        const res = await sendPortfolioOverview(baseCurrency);
        if (res.sent) {
          setStatus({ kind: "sent" });
        } else {
          const minutes = Math.max(1, Math.ceil((res.retry_after_seconds ?? 0) / 60));
          setStatus({ kind: "cooldown", minutes });
        }
      } catch {
        setStatus({ kind: "error" });
      }
    });
  };

  return (
    <div className="flex flex-col items-end gap-1">
      <Button onClick={handleClick} disabled={isPending} variant="outline" size="sm">
        {isPending ? t("sendOverviewSending") : t("sendOverviewButton")}
      </Button>
      {status.kind === "sent" && (
        <p className="text-xs text-muted-foreground">{t("sendOverviewSuccess")}</p>
      )}
      {status.kind === "cooldown" && (
        <p className="text-xs text-muted-foreground">
          {t("sendOverviewCooldown", { minutes: status.minutes })}
        </p>
      )}
      {status.kind === "error" && (
        <p role="alert" className="text-xs text-destructive">
          {t("sendOverviewError")}
        </p>
      )}
    </div>
  );
}
