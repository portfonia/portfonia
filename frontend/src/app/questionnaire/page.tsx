import { getInvestmentContextServer } from "@/lib/server-api";
import type { InvestmentContext } from "@/lib/api";
import { QuestionnairePageBody } from "./_components/questionnaire-page-body";

export default async function QuestionnairePage() {
  let initialContext: InvestmentContext | null = null;
  let hadLoadError = false;
  try {
    initialContext = await getInvestmentContextServer();
  } catch {
    hadLoadError = true;
  }

  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <QuestionnairePageBody initialContext={initialContext} hadLoadError={hadLoadError} />
    </main>
  );
}
