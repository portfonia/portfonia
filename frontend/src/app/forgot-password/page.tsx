import { ForgotPasswordHeading } from "./forgot-password-heading";
import { ForgotPasswordForm } from "./forgot-password-form";

export default function ForgotPasswordPage() {
  return (
    <main className="mx-auto flex max-w-lg flex-col gap-8 px-4 py-24">
      <ForgotPasswordHeading />
      <ForgotPasswordForm />
    </main>
  );
}
