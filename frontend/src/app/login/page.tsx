import { ExpiredSessionBanner } from "@/components/expired-session-banner";
import { LoginHeading } from "./login-heading";
import { LoginForm } from "./login-form";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string; next?: string }>;
}) {
  const { reason, next } = await searchParams;

  return (
    <main className="mx-auto flex max-w-lg flex-col gap-8 px-4 py-24">
      <LoginHeading />
      <ExpiredSessionBanner reason={reason} />
      <LoginForm next={next} />
    </main>
  );
}
