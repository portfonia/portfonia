import { SignupHeading } from "./signup-heading";
import { SignupForm } from "./signup-form";

export default async function SignupPage({
  searchParams,
}: {
  searchParams: Promise<{ invite?: string }>;
}) {
  const { invite } = await searchParams;
  let lockedEmail: string | null = null;
  if (invite) {
    try {
      const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8000";
      const response = await fetch(
        `${backendUrl}/auth/invite-email?token=${encodeURIComponent(invite)}`,
        { cache: "no-store" },
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
    </main>
  );
}
