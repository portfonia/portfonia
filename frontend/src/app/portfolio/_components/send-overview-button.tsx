"use client";

import { useState, useTransition } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { sendPortfolioOverview } from "@/lib/api";

// issue #202: explicit, user-clicked "Send holdings overview" — NOT a
// formal report. The 15-minute cooldown is enforced server-side
// (check_portfolio_overview_cooldown); this component only renders whatever
// the endpoint reports back, it does not run its own client-side timer.
export function SendOverviewButton({
  baseCurrency,
  disabled = false,
}: {
  baseCurrency: string;
  // Set while the page's own currency switch is in flight (review
  // 5100733033 leftover): the caller passes the settled summary.
  // base_currency, which already prevents a stale-currency send, but
  // disabling too avoids a click landing between "switch requested" and
  // "summary updated" from reading as a no-op to the user.
  disabled?: boolean;
}) {
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
        } else if (res.retry_after_seconds != null) {
          setStatus({ kind: "cooldown", minutes: Math.max(1, Math.ceil(res.retry_after_seconds / 60)) });
        } else {
          // sent=false with no retry_after_seconds means the dispatch
          // itself failed (server released the cooldown claim) — not a
          // cooldown, an actual failure, so it gets the error message.
          setStatus({ kind: "error" });
        }
      } catch {
        setStatus({ kind: "error" });
      }
    });
  };

  return (
    <div className="flex flex-col items-end gap-1">
      <Button onClick={handleClick} disabled={isPending || disabled} variant="outline" size="sm">
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
