import { LoginHandoff } from "@/app/_components/login-handoff";
import {
  ForbiddenShell,
  ManagementShell,
  UnavailableShell,
} from "@/app/_components/management-shell";
import { getVaultServer } from "@/lib/server-api";
import { createClient } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

export default async function ManagementPage() {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) {
    return <LoginHandoff />;
  }

  const loaded = await getVaultServer();
  if (loaded.status === "unauthenticated") {
    return <LoginHandoff />;
  }
  if (loaded.status === "forbidden") {
    return <ForbiddenShell />;
  }
  if (loaded.status === "unavailable") {
    return <UnavailableShell />;
  }
  return <ManagementShell vault={loaded.vault} />;
}
