// Server-side data access. Server Components cannot use the browser `/api`
// rewrite proxy, so they call the backend directly via BACKEND_URL. Client-side
// mutations still go through the same-origin `/api` proxy (see lib/api.ts).
import type { HoldingOut } from "@/lib/api";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export async function listHoldingsServer(): Promise<HoldingOut[]> {
  const res = await fetch(`${BACKEND_URL}/holdings`, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`Backend returned ${res.status}`);
  }
  return res.json() as Promise<HoldingOut[]>;
}
