"use client";

import { useTranslations } from "next-intl";

import type { InvestmentContext } from "@/lib/api";
import { QuestionnaireForm } from "./questionnaire-form";

// Split out from page.tsx (issue #209) — see app/login/login-heading.tsx for
// why. Unlike the login/signup/holdings headings, this one also owns the
// load-error branch: whether the form renders at all depends on translated
// content (the error message), so it isn't worth carving that condition back
// into the Server Component just to keep this file heading-only.
export function QuestionnairePageBody({
  initialContext,
  hadLoadError,
  mode = "edit",
}: {
  initialContext: InvestmentContext | null;
  hadLoadError: boolean;
  mode?: "onboarding" | "edit";
}) {
  const t = useTranslations("questionnaire");
  // Ring 1-Onboarding.md §2.2: onboarding gets its own heading/subtitle;
  // edit keeps the original copy.
  const heading = mode === "onboarding" ? t("onboardingPageTitle") : t("pageTitle");
  const subtitle = mode === "onboarding" ? t("onboardingPageSubtitle") : t("pageSubtitle");
  return (
    <>
      <header className="mb-8">
        <h1 className="font-heading text-2xl font-semibold">{heading}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>
      </header>
      {hadLoadError ? (
        <p className="text-sm text-destructive" role="alert">
          {t("errorLoadFailed")}
        </p>
      ) : (
        <QuestionnaireForm initialContext={initialContext} mode={mode} />
      )}
    </>
  );
}
