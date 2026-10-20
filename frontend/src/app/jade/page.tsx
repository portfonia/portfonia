import { currentAccessToken } from "@/lib/supabase/server";
import { calculateExample } from "@/app/guides/_lib/example";
import { stressExample, styleExample, trackingExample } from "@/app/guides/_lib/example-batch2";
import { whatIfExample } from "@/app/guides/_lib/example-what-if";
import type { Me } from "@/lib/api";
import { getMeServer } from "@/lib/server-api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { JadePageBody } from "./_components/jade-page-body";

export default async function JadePage() {
  const token = await currentAccessToken();
  const guest = !token;
  let me: Me | null = null;
  let hadLoadError = false;
  if (token) try {
    me = await getMeServer();
  } catch (err) {
    if (isNextRedirectError(err)) throw err;
    hadLoadError = true;
  }
  const isJade = me?.subscription.status === "active" && me.subscription.type === "jade";
  const demo = guest || !isJade ? { example: calculateExample(), stress: stressExample(), style: styleExample(), tracking: trackingExample(), whatIf: whatIfExample() } : null;
  return <main className="mx-auto w-full max-w-2xl px-4 py-10 sm:px-6"><JadePageBody guest={guest} demo={demo} me={me} hadLoadError={hadLoadError} /></main>;
}
