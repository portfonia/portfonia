import { UnsubscribeForm } from "./unsubscribe-form";

// Public route (proxy.ts PUBLIC_PATH_PREFIXES) — the token itself is the
// credential (design doc §3.7), same as /verify-email. No Server Component
// auth check here.
//
// No next-intl usage in this file: locale lives in client-only context
// (locale-provider). All copy lives in UnsubscribeForm below.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export interface UnsubscribeStatus {
  found: boolean;
  email: string | null;
}

async function fetchStatus(token: string): Promise<UnsubscribeStatus> {
  try {
    const res = await fetch(
      `${BACKEND_URL}/unsubscribe/status?token=${encodeURIComponent(token)}`,
      { cache: "no-store" },
    );
    if (!res.ok) return { found: false, email: null };
    return (await res.json()) as UnsubscribeStatus;
  } catch {
    return { found: false, email: null };
  }
}

export default async function UnsubscribePage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const { token } = await searchParams;
  const status = token ? await fetchStatus(token) : { found: false, email: null };

  return (
    <main className="mx-auto flex w-full max-w-sm flex-col gap-4 px-6 py-10">
      <UnsubscribeForm token={token ?? ""} status={status} />
    </main>
  );
}
