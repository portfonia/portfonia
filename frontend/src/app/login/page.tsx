import { ExpiredSessionBanner } from "@/components/expired-session-banner";
import { LoginHeading } from "./login-heading";
import { LoginForm } from "./login-form";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string; next?: string }>;
}) {
  const { reason, next } = await searchParams;
  // Keep the hidden return destination constrained to the same exact
  // allowlist that login/actions.ts enforces server-side.
  const validatedNext =
    next === "/vigil" || next === "/vigil/setup" || next === "/vigil/activate"
      ? next
      : undefined;

  return (
    <main className="mx-auto flex max-w-lg flex-col gap-8 px-4 py-24">
      <LoginHeading />
      <ExpiredSessionBanner reason={reason} />
      <LoginForm next={validatedNext} />
    </main>
  );
}
