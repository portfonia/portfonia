"use client";

import { useTranslations } from "next-intl";

import { SESSION_IDLE_TIMEOUT_MS } from "@/lib/idle-timeout";

// Single source of truth for the idle-logout timeout (issue #207 R6),
// shared with hooks/use-idle-logout.ts so the copy cannot drift from
// enforcement.
const idleMinutes = Math.round(SESSION_IDLE_TIMEOUT_MS / 60_000);

// Shown on /login when the user lands there from an idle auto-logout
// (?reason=expired). Silent by design while logged in (OQ-5) — this banner is
// the only feedback the flow gives.
export function ExpiredSessionBanner({ reason }: { reason?: string }) {
  const t = useTranslations("menu");
  if (reason !== "expired") return null;

  return (
    <div
      role="status"
      className="rounded-lg border border-border bg-muted px-4 py-3 text-sm text-foreground/80"
    >
      {t("sessionExpired", { minutes: idleMinutes })}
    </div>
  );
}
