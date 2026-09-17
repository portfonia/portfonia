import { ExpiredSessionBanner } from "@/components/expired-session-banner";
import { LoginHeading } from "./login-heading";
import { LoginForm } from "./login-form";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string; next?: string }>;
}) {
  const { reason, next } = await searchParams;
  // Second, independent check on top of login/actions.ts's own
  // resolveNextDestination (issue #453) — belt and suspenders, not a
  // substitute: only the exact literal ever reaches the hidden form field.
  const validatedNext = next === "/vigil" ? "/vigil" : undefined;

  return (
    <main className="mx-auto flex max-w-lg flex-col gap-8 px-4 py-24">
      <LoginHeading />
      <ExpiredSessionBanner reason={reason} />
      <LoginForm next={validatedNext} />
    </main>
  );
}
