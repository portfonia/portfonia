import { messages } from "@/lib/messages";

// Shown on /login when the user lands there from an idle auto-logout
// (?reason=expired). Silent by design while logged in (OQ-5) — this banner is
// the only feedback the flow gives.
export function ExpiredSessionBanner({ reason }: { reason?: string }) {
  if (reason !== "expired") return null;

  return (
    <div
      role="status"
      className="rounded-lg border border-border bg-muted px-4 py-3 text-sm text-foreground/80"
    >
      {messages.menu.sessionExpired}
    </div>
  );
}
