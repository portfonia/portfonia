import { RevokeResult } from "./revoke-result";

export default async function RevokePage({ searchParams }: { searchParams: Promise<{ t?: string }> }) {
  const { t } = await searchParams;
  return <main className="mx-auto w-full max-w-lg px-6 py-10"><RevokeResult token={t ?? ""} /></main>;
}
