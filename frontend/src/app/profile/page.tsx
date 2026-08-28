import type { Me } from "@/lib/api";
import { getMeServer } from "@/lib/server-api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { ProfilePageBody } from "./_components/profile-page-body";

export default async function ProfilePage() {
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
      <ProfilePageBody me={me} hadLoadError={hadLoadError} />
    </main>
  );
}
