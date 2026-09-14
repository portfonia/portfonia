import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";
import { portfoniaSessionIsActive } from "@/lib/server-api";

import { VigilBridgeClient } from "./bridge-client";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "Continue to Vigil",
  robots: { index: false, follow: false },
};

export default async function VigilBridgePage() {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) {
    redirect("/login?next=/auth/vigil");
  }
  const active = await portfoniaSessionIsActive();
  if (!active) {
    redirect("/login?next=/auth/vigil");
  }
  return <VigilBridgeClient email={user.email ?? ""} />;
}
