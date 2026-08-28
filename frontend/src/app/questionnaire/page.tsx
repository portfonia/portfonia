import { getInvestmentContextServer } from "@/lib/server-api";
import type { InvestmentContext } from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { QuestionnairePageBody } from "./_components/questionnaire-page-body";

export default async function QuestionnairePage({
  searchParams,
}: {
  searchParams: Promise<{ onboarding?: string }>;
}) {
  let initialContext: InvestmentContext | null = null;
  let hadLoadError = false;
  try {
    initialContext = await getInvestmentContextServer();
  } catch (err) {
    // A 401 here can be the idle-logout Server Action's own redirect()
    // throw (issue #235/#240) — that must propagate, not be swallowed.
    if (isNextRedirectError(err)) throw err;
    hadLoadError = true;
  }
  // mode="onboarding" has exactly one trigger point: the post-signup
  // redirect in signup/actions.ts (Ring 1-Onboarding.md §2.1). The Profile
  // gap card links here with no query string, which falls through to the
  // "edit" default.
  const { onboarding } = await searchParams;
  const mode = onboarding === "1" ? "onboarding" : "edit";

  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <QuestionnairePageBody
        initialContext={initialContext}
        hadLoadError={hadLoadError}
        mode={mode}
      />
    </main>
  );
}
