import type { Me } from "@/lib/api";
import { getMeServer } from "@/lib/server-api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { WelcomeBody } from "./_components/welcome-body";

// Not a public path (proxy.ts PUBLIC_PATH_PREFIXES) — an unauthenticated
// request redirects to /login with no route-specific code, same mechanism
// as /profile and /holdings.
export default async function WelcomePage() {
  let me: Me | null = null;
  let hadLoadError = false;
  try {
    me = await getMeServer();
  } catch (err) {
    // A 401 here can be the idle-logout Server Action's own redirect()
    // throw (issue #235/#240) — that must propagate, not be swallowed.
    if (isNextRedirectError(err)) throw err;
    hadLoadError = true;
  }

  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <WelcomeBody me={me} hadLoadError={hadLoadError} />
    </main>
  );
}
