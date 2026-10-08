import type { Metadata } from "next";
import { NOINDEX_METADATA } from "@/lib/seo";

import { ResetPasswordHeading } from "./reset-password-heading";
import { ResetPasswordForm } from "./reset-password-form";

export default function ResetPasswordPage() {
  return (
    <main className="mx-auto flex max-w-lg flex-col gap-8 px-4 py-24">
      <ResetPasswordHeading />
      <ResetPasswordForm />
    </main>
  );
}

export const metadata: Metadata = { ...NOINDEX_METADATA };
