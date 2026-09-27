import { WaitlistForm } from "./waitlist-form";
import { WaitlistHeading } from "./waitlist-heading";

export default function WaitlistPage() {
  return (
    <main className="mx-auto flex max-w-lg flex-col gap-8 px-4 py-24">
      <WaitlistHeading />
      <WaitlistForm />
    </main>
  );
}
