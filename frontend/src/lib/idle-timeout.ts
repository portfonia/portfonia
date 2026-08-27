// Single source of truth for the client-side idle-logout timeout (issue
// #207 R6). Imported by both hooks/use-idle-logout.ts (the enforcement) and
// components/expired-session-banner.tsx (the user-facing copy), so the two
// cannot drift.
export const SESSION_IDLE_TIMEOUT_MS = 15 * 60_000;
