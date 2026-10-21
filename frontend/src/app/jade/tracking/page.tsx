import type { Me } from "@/lib/api";
import { getMeServer } from "@/lib/server-api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { TrackingBody } from "./tracking-body";
export default async function Page() {
  let me: Me | null = null;
  let hadLoadError = false;
  try { me = await getMeServer(); } catch (err) { if (isNextRedirectError(err)) throw err; hadLoadError = true; }
  return <main className="mx-auto w-full max-w-5xl px-4 py-10 sm:px-6"><TrackingBody me={me} hadLoadError={hadLoadError} /></main>;
}
