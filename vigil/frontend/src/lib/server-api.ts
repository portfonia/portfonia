import { bearerAuthHeaders } from "@/lib/supabase/server";
import type { VaultLoad, VaultView } from "@/lib/vault";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export async function getVaultServer(): Promise<VaultLoad> {
  const res = await fetch(`${BACKEND_URL}/vault`, {
    cache: "no-store",
    headers: await bearerAuthHeaders(),
  });
  if (res.status === 401) return { status: "unauthenticated" };
  if (res.status === 403) return { status: "forbidden" };
  if (res.status === 503) return { status: "unavailable" };
  if (!res.ok) return { status: "unavailable" };
  const vault = (await res.json()) as VaultView;
  return { status: "ok", vault };
}
