import { VerifyEmailForm } from "./verify-email-form";

// Public route (proxy.ts PUBLIC_PATH_PREFIXES) — the token itself is the
// credential (design doc §3.3/§6.3 precedent in Vigil Concept & Design),
// same as /reset-password. No Server Component auth check here.
//
// No next-intl usage in this file: this app has no URL-based locale routing
// (issue #209) and locale lives in client-only context (locale-provider) —
// forgot-password/page.tsx (the page this mirrors) reads zero translations
// server-side for the same reason. All copy lives in VerifyEmailForm below.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export interface VerifyEmailStatus {
  found: boolean;
  status: string | null;
  email: string | null;
}

// GET-inert lookup only — no state change (design doc §3.3 step 2 / Vigil
// §4.2: an email security gateway's link-prefetch must never look like a
// confirmation). A network/parse failure here degrades to the same
// "invalid or expired" message the form itself shows for a bad token —
// there's nothing more specific to tell an anonymous visitor.
async function fetchStatus(token: string): Promise<VerifyEmailStatus> {
  try {
    const res = await fetch(
      `${BACKEND_URL}/email-verifications/status?token=${encodeURIComponent(token)}`,
      { cache: "no-store" },
    );
    if (!res.ok) return { found: false, status: null, email: null };
    return (await res.json()) as VerifyEmailStatus;
  } catch {
    return { found: false, status: null, email: null };
  }
}

export default async function VerifyEmailPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const { token } = await searchParams;
  const status = token ? await fetchStatus(token) : { found: false, status: null, email: null };

  return (
    <main className="mx-auto flex w-full max-w-sm flex-col gap-4 px-6 py-10">
      <VerifyEmailForm token={token ?? ""} status={status} />
    </main>
  );
}
