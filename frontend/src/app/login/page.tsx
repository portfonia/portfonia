import { messages } from "@/lib/messages";
import { LoginForm } from "./login-form";

const m = messages.auth;

export default function LoginPage() {
  return (
    <main className="mx-auto flex max-w-lg flex-col gap-8 px-4 py-24">
      <div className="text-center">
        <h1 className="font-serif text-3xl">{m.loginHeading}</h1>
        <p className="mt-2 text-sm text-foreground/60">{m.loginSubtitle}</p>
      </div>
      <LoginForm />
    </main>
  );
}
