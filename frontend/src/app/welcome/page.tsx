import type { Me } from "@/lib/api";
import { getMeServer } from "@/lib/server-api";
import { WelcomeBody } from "./_components/welcome-body";

// Not a public path (proxy.ts PUBLIC_PATH_PREFIXES) — an unauthenticated
// request redirects to /login with no route-specific code, same mechanism
// as /profile and /holdings.
export default async function WelcomePage() {
  let me: Me | null = null;
  let hadLoadError = false;
  try {
    me = await getMeServer();
  } catch {
    hadLoadError = true;
  }

  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <WelcomeBody me={me} hadLoadError={hadLoadError} />
    </main>
  );
}
