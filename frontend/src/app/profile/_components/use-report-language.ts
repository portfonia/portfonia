"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useTranslations } from "next-intl";

import { updateReportLanguage } from "@/lib/api";
import type { ReportLanguage } from "@/locales";

// Profile page's Report Language control (issue #308): unlike the
// report_cadence placeholder, this one is fully wired — saves immediately
// on change (no separate Save button, matching the resend/send flows'
// discipline) and calls router.refresh() on success, never a hard
// window.location.reload() (that exact mistake was already made and fixed
// twice on this page — PR #263, PR #292).
export function useReportLanguage() {
  const t = useTranslations("profile");
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleChange(reportLanguage: ReportLanguage) {
    setPending(true);
    setError(null);
    try {
      await updateReportLanguage(reportLanguage);
      router.refresh();
    } catch {
      // No status-specific copy (unlike the verification resend/send
      // flows): this is a plain authenticated write with no rate limiting
      // or external side effect, so every failure gets the same message.
      setError(t("reportLanguageError"));
    } finally {
      setPending(false);
    }
  }

  return { pending, error, handleChange };
}
