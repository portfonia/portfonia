import { getTechnicalTableServer } from "@/lib/server-api";
import type { TechnicalTableOut } from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { TechnicalPageBody } from "./_components/technical-page-body";

export default async function TechnicalPage() {
  let initialTable: TechnicalTableOut | null = null;
  let initialLoadError = false;
  try {
    initialTable = await getTechnicalTableServer();
  } catch (err) {
    if (isNextRedirectError(err)) throw err;
    initialLoadError = true;
  }
  return <main className="mx-auto w-full max-w-5xl px-6 py-10"><TechnicalPageBody initialTable={initialTable} initialLoadError={initialLoadError} /></main>;
}
