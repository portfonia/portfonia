import { SignupHeading } from "./signup-heading";
import { SignupForm } from "./signup-form";

export default async function SignupPage({
  searchParams,
}: {
  searchParams: Promise<{ invite?: string }>;
}) {
  const { invite } = await searchParams;

  return (
    <main className="mx-auto flex max-w-lg flex-col gap-8 px-4 py-24">
      <SignupHeading />
      <SignupForm inviteToken={invite ?? ""} />
    </main>
  );
}
