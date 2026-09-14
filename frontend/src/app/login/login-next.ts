// Split out of actions.ts (2026-09-14): a "use server" file may only export
// async functions — a client component importing a plain constant or a
// synchronous function from one breaks in a real `next build` (Turbopack
// treats the whole module as export-less), even though `tsc --noEmit` and
// the vitest transform don't catch it. Keep these here, not in actions.ts.

export const LOGIN_NEXT_PROFILE = "/profile";
export const LOGIN_NEXT_VIGIL = "/auth/vigil";

// Exact-path allowlist only. Prefix, substring, absolute, and encoded
// values all fall through to the default /profile landing.
export function resolveLoginNext(raw: unknown): "/profile" | "/auth/vigil" {
  return raw === LOGIN_NEXT_VIGIL ? LOGIN_NEXT_VIGIL : LOGIN_NEXT_PROFILE;
}
