import "@testing-library/jest-dom/vitest";

// NEXT_PUBLIC_* vars are normally inlined at build time (see
// frontend/Dockerfile) — they're genuinely unset under vitest, so
// supabasePublicEnv() (lib/supabase/env.ts) would throw before any test
// touching proxy.ts/browser.ts/server.ts even reached its own mocks.
// Dummy, non-secret test values; individual tests override via
// process.env when they need to exercise the missing/blank cases
// (see lib/supabase/env.test.ts).
process.env.NEXT_PUBLIC_SUPABASE_URL ??= "https://auth.test.local";
process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ??= "sb_publishable_test";
