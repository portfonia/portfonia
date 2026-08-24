import { messages } from "@/lib/messages";
import { SignupForm } from "./signup-form";

const m = messages.auth;

export default async function SignupPage({
  searchParams,
}: {
  searchParams: Promise<{ invite?: string }>;
}) {
  const { invite } = await searchParams;

  return (
    <main className="mx-auto flex max-w-lg flex-col gap-8 px-4 py-24">
      <div className="text-center">
        <h1 className="font-serif text-3xl">{m.signupHeading}</h1>
        <p className="mt-2 text-sm text-foreground/60">{m.signupSubtitle}</p>
      </div>
      <SignupForm inviteToken={invite ?? ""} />
    </main>
  );
}
