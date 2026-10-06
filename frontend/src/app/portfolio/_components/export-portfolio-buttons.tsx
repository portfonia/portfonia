"use client";

import { useState, useTransition } from "react";
import { useTranslations } from "next-intl";

import { useLocale } from "@/app/_components/locale-provider";
import { exportPortfolio, type PortfolioExportFormat } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { DownloadConfirmDialog, type PendingDownload } from "@/components/download-confirm-dialog";

// UI locales map to export codes: zh-Hans -> "zh", zh-Hant -> "zh-Hant".
// Other values pass through; unrecognized codes fall back to English server-side.
function exportLocaleParam(locale: string): string {
  if (locale === "zh-Hans") return "zh";
  if (locale === "zh-Hant") return "zh-Hant";
  return locale;
}

// Unrecognized export codes fall back to English server-side, so the dialog
// names English for them too.
function exportLanguageKey(exportLocale: string): "en" | "zh" | "zh-Hant" {
  return exportLocale === "zh" || exportLocale === "zh-Hant" ? exportLocale : "en";
}

export function ExportPortfolioButtons({
  baseCurrency,
  disabled = false,
}: {
  baseCurrency: string;
  disabled?: boolean;
}) {
  const t = useTranslations("portfolio");
  const tConfirm = useTranslations("downloadConfirm");
  const { locale } = useLocale();
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState(false);
  const [pending, setPending] = useState<PendingDownload | null>(null);

  function handleDownload(format: PortfolioExportFormat) {
    setError(false);
    startTransition(async () => {
      try {
        const exportLocale = exportLocaleParam(locale);
        const { blob, filename } = await exportPortfolio(format, baseCurrency, exportLocale);
        setPending({
          blob,
          filename,
          description: t("exportDescription", {
            currency: baseCurrency,
            language: tConfirm(`languages.${exportLanguageKey(exportLocale)}`),
          }),
        });
      } catch {
        setError(true);
      }
    });
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex flex-wrap justify-end gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={isPending || disabled}
          onClick={() => handleDownload("xlsx")}
        >
          {t("exportXlsxButton")}
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={isPending || disabled}
          onClick={() => handleDownload("md")}
        >
          {t("exportMdButton")}
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {t("exportError")}
        </p>
      )}
      <DownloadConfirmDialog pending={pending} onClose={() => setPending(null)} />
    </div>
  );
}
