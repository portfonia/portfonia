"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useTranslations } from "next-intl";

import { updateReportCurrency } from "@/lib/api";
import type { BaseCurrency } from "@/app/portfolio/_components/currencies";

// Profile page's Report Currency control (issue #350 item 1) — same
// save-immediately-on-change, router.refresh()-not-reload shape as
// use-report-language.ts's useReportLanguage. See that hook's docstring
// for why a hard window.location.reload() must never come back.
export function useReportCurrency() {
  const t = useTranslations("profile");
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleChange(reportCurrency: BaseCurrency) {
    setPending(true);
    setError(null);
    try {
      await updateReportCurrency(reportCurrency);
      router.refresh();
    } catch {
      setError(t("reportCurrencyError"));
    } finally {
      setPending(false);
    }
  }

  return { pending, error, handleChange };
}
