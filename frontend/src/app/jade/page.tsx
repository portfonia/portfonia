import type { Me } from "@/lib/api";
import { getMeServer } from "@/lib/server-api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { JadePageBody } from "./_components/jade-page-body";

export default async function JadePage() {
  let me: Me | null = null;
  let hadLoadError = false;
  try {
    me = await getMeServer();
  } catch (err) {
    if (isNextRedirectError(err)) throw err;
    hadLoadError = true;
  }
  return <main className="mx-auto w-full max-w-2xl px-4 py-10 sm:px-6"><JadePageBody me={me} hadLoadError={hadLoadError} /></main>;
}
