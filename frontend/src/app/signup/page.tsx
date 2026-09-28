import { headers } from "next/headers";

import { SignupHeading } from "./signup-heading";
import { SignupForm } from "./signup-form";
import { SignupLang } from "./signup-lang";

export default async function SignupPage({
  searchParams,
}: {
  searchParams: Promise<{ invite?: string; lang?: string }>;
}) {
  const { invite, lang } = await searchParams;
  let lockedEmail: string | null = null;
  if (invite) {
    try {
      const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8000";
      // Match signup/actions.ts: the backend sees the frontend container as
      // the peer unless Caddy's visitor IP is forwarded through this hop.
      const incoming = await headers();
      const forwarded: Record<string, string> = {};
      const xff = incoming.get("x-forwarded-for");
      const realIp = incoming.get("x-real-ip");
      if (xff) forwarded["X-Forwarded-For"] = xff;
      if (realIp) forwarded["X-Real-IP"] = realIp;
      const response = await fetch(
        `${backendUrl}/auth/invite-email?token=${encodeURIComponent(invite)}`,
        { cache: "no-store", headers: forwarded },
      );
      if (response.ok) {
        const body: { email: string | null } = await response.json();
        lockedEmail = body.email;
      }
    } catch {
      lockedEmail = null;
    }
  }

  return (
    <main className="mx-auto flex max-w-lg flex-col gap-8 px-4 py-24">
      <SignupHeading />
      <SignupForm inviteToken={invite ?? ""} lockedEmail={lockedEmail} />
      <SignupLang initialLang={lang ?? null} />
    </main>
  );
}
