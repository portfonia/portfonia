import { isNextRedirectError } from "@/lib/next-redirect-error";
import { getVigilVaultStatusServer, type VigilVaultLoadResult } from "@/lib/vigil/server";
import { VigilPageBody } from "./_components/vigil-page-body";

// Protected by the default proxy.ts gate (not in PUBLIC_PATH_PREFIXES, not
// one of the three exact public shells) — no route-specific auth code
// needed, same as /profile and /holdings. Real authorization is
// require_vigil_owner on the backend; this page's own load just decides
// what to render.
export default async function VigilPage() {
  let result: VigilVaultLoadResult;
  try {
    result = await getVigilVaultStatusServer();
  } catch (err) {
    // A 401 here can be the server-side idle/absolute-session-lifetime
    // check's own redirect() throw (issue #235/#236) — that must propagate,
    // not be swallowed into an "unavailable" card.
    if (isNextRedirectError(err)) throw err;
    result = { status: "error" };
  }

  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <VigilPageBody result={result} />
    </main>
  );
}
