// Server-side data access. Server Components cannot use the browser `/api`
// rewrite proxy, so they call the backend directly via BACKEND_URL. Client-side
// mutations still go through the same-origin `/api` proxy (see lib/api.ts).
//
// This runs in Node, not the browser, so it never automatically carries the
// Supabase session cookie the way a same-origin browser fetch does (Ring
// 1-B design doc §7.3(1)) — it must derive its own Authorization header,
// same reasoning as the upload Route Handler (see
// app/api/holdings/upload/route.ts).
import type { HoldingOut, InvestmentContext, Me } from "@/lib/api";
import { currentAccessToken } from "@/lib/supabase/server";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

async function authHeaders(): Promise<HeadersInit> {
  const token = await currentAccessToken();
  return token ? { authorization: `Bearer ${token}` } : {};
}

export async function listHoldingsServer(): Promise<HoldingOut[]> {
  const res = await fetch(`${BACKEND_URL}/holdings`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) {
    throw new Error(`Backend returned ${res.status}`);
  }
  return res.json() as Promise<HoldingOut[]>;
}

// §8.4: 404 means "never answered" — not an error the caller should surface.
export async function getInvestmentContextServer(): Promise<InvestmentContext | null> {
  const res = await fetch(`${BACKEND_URL}/investment-context`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (res.status === 404) return null;
  if (!res.ok) {
    throw new Error(`Backend returned ${res.status}`);
  }
  return res.json() as Promise<InvestmentContext>;
}

export async function getMeServer(): Promise<Me> {
  const res = await fetch(`${BACKEND_URL}/me`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) {
    throw new Error(`Backend returned ${res.status}`);
  }
  return res.json() as Promise<Me>;
}
