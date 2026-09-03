"use client";

import { useState, useTransition } from "react";
import { useTranslations } from "next-intl";

import { useLocale } from "@/app/_components/locale-provider";
import { exportPortfolio, type PortfolioExportFormat } from "@/lib/api";
import { downloadFile } from "@/lib/template";
import { Button } from "@/components/ui/button";

// issue #331: user-triggered snapshot download, not a delivery mechanism
// (distinct from SendOverviewButton). `locale` maps the same way exportLocaleParam
// does in holdings-manager.tsx: zh-Hans -> "zh", everything else passes through
// literally and falls back to English server-side.
function exportLocaleParam(locale: string): string {
  return locale === "zh-Hans" ? "zh" : locale;
}

export function ExportPortfolioButtons({
  baseCurrency,
  disabled = false,
}: {
  baseCurrency: string;
  disabled?: boolean;
}) {
  const t = useTranslations("portfolio");
  const { locale } = useLocale();
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState(false);

  function handleDownload(format: PortfolioExportFormat) {
    setError(false);
    startTransition(async () => {
      try {
        const { blob, filename } = await exportPortfolio(
          format,
          baseCurrency,
          exportLocaleParam(locale),
        );
        downloadFile(blob, filename);
      } catch {
        setError(true);
      }
    });
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex gap-2">
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
    </div>
  );
}
