import { messages } from "@/lib/messages";
import { getInvestmentContextServer } from "@/lib/server-api";
import type { InvestmentContext } from "@/lib/api";
import { QuestionnaireForm } from "./_components/questionnaire-form";

const m = messages.questionnaire;

export default async function QuestionnairePage() {
  let initialContext: InvestmentContext | null = null;
  let loadError: string | null = null;
  try {
    initialContext = await getInvestmentContextServer();
  } catch {
    loadError = m.errorLoadFailed;
  }

  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <header className="mb-8">
        <h1 className="font-heading text-2xl font-semibold">{m.pageTitle}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{m.pageSubtitle}</p>
      </header>
      {loadError ? (
        <p className="text-sm text-destructive" role="alert">
          {loadError}
        </p>
      ) : (
        <QuestionnaireForm initialContext={initialContext} />
      )}
    </main>
  );
}
