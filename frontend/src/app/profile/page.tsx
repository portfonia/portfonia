import type { Me } from "@/lib/api";
import { getMeServer } from "@/lib/server-api";
import { ProfilePageBody } from "./_components/profile-page-body";

export default async function ProfilePage() {
  let me: Me | null = null;
  let hadLoadError = false;
  try {
    me = await getMeServer();
  } catch {
    hadLoadError = true;
  }

  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <ProfilePageBody me={me} hadLoadError={hadLoadError} />
    </main>
  );
}
