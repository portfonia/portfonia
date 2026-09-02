"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import {
  ApiError,
  confirmHoldings,
  downloadHoldingsTemplate,
  exportHoldings,
  uploadHoldings,
  type ConfirmMode,
  type HoldingOut,
  type UploadPreview,
} from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { downloadFile } from "@/lib/template";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { HoldingsTable } from "./holdings-table";
import { BrokerSummary, IssueList, PreviewTable, rowNeedsAmber } from "./preview";

export function HoldingsManager({
  initialHoldings,
  initialLoadError = false,
  mode = "normal",
}: {
  initialHoldings: HoldingOut[];
  initialLoadError?: boolean;
  mode?: "onboarding" | "normal";
}) {
  const t = useTranslations("holdings");
  const router = useRouter();
  const [holdings, setHoldings] = useState<HoldingOut[]>(initialHoldings);
  const [preview, setPreview] = useState<UploadPreview | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadSeconds, setUploadSeconds] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [issuesConfirmOpen, setIssuesConfirmOpen] = useState(false);
  const [replaceConfirmOpen, setReplaceConfirmOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const displayError = error ?? (initialLoadError ? t("errorLoadFailed") : null);

  useEffect(() => {
    if (!uploading) return;
    const id = setInterval(() => setUploadSeconds((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [uploading]);

  function uploadingProgressText(seconds: number): string {
    if (seconds < 5) return t("uploadingProgress.reading");
    if (seconds < 20) return t("uploadingProgress.parsing");
    if (seconds < 45) return t("uploadingProgress.stillWorking", { seconds });
    return t("uploadingProgress.slow", { seconds });
  }

  async function onFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setError(null);
    setUploadSeconds(0);
    setUploading(true);
    setPreview(null);
    try {
      setPreview(await uploadHoldings(file));
    } catch (err) {
      if (isNextRedirectError(err)) throw err;
      setError(
        `${t("errorUploadFailed")}: ${err instanceof ApiError ? err.message : String(err)}`,
      );
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function doSave(confirmMode: ConfirmMode) {
    if (!preview) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await confirmHoldings(preview.valid_rows, confirmMode);
      if (mode === "onboarding") {
        router.push("/welcome");
        return;
      }
      setHoldings(saved);
      setPreview(null);
    } catch (err) {
      if (isNextRedirectError(err)) throw err;
      setError(
        `${t("errorSaveFailed")}: ${err instanceof ApiError ? err.message : String(err)}`,
      );
    } finally {
      setSaving(false);
      setIssuesConfirmOpen(false);
      setReplaceConfirmOpen(false);
    }
  }

  function onAppendClick() {
    if (!preview) return;
    if (preview.issue_rows.length > 0) {
      setIssuesConfirmOpen(true);
    } else {
      void doSave("append");
    }
  }

  function onReplaceClick() {
    if (!preview) return;
    setReplaceConfirmOpen(true);
  }

  async function onExport() {
    try {
      const exported = await exportHoldings();
      downloadFile(exported.blob, exported.filename);
    } catch (err) {
      if (isNextRedirectError(err)) throw err;
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function onDownloadTemplate() {
    try {
      downloadFile(await downloadHoldingsTemplate(), "holdings-template.md");
    } catch (err) {
      if (isNextRedirectError(err)) throw err;
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  const hasInferred =
    preview?.valid_rows.some((r) => rowNeedsAmber(r)) ?? false;

  return (
    <>
      {displayError && (
        <div className="mb-6 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {displayError}
        </div>
      )}

      {mode === "onboarding" && (
        <div className="mb-6 flex justify-end">
          <Link href="/welcome" className="text-sm underline-offset-4 hover:underline">
            {t("skipOnboarding")}
          </Link>
        </div>
      )}

      <Card className="mb-6">
        <CardHeader>
          <CardTitle>{t("uploadHeading")}</CardTitle>
          <CardDescription>{t("uploadHint")}</CardDescription>
          {mode !== "onboarding" && (
            <CardAction>
              <Button variant="outline" size="sm" onClick={() => void onDownloadTemplate()}>
                {t("downloadTemplate")}
              </Button>
            </CardAction>
          )}
        </CardHeader>
        <CardContent>
          <input
            ref={fileInputRef}
            type="file"
            accept=".md,.txt,.csv,.xlsx,.xls"
            className="hidden"
            onChange={onFileChange}
          />
          <div className="flex items-center gap-3">
            <Button
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
            >
              {uploading ? t("uploading") : t("chooseFile")}
            </Button>
            {uploading && (
              <span className="text-sm text-muted-foreground">
                {uploadingProgressText(uploadSeconds)}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {preview && (
        <Card className="mb-6">
          <CardHeader>
            <CardTitle>{t("previewHeading")}</CardTitle>
            <CardDescription>
              {t("previewValidCount", { n: preview.valid_rows.length })}
              {hasInferred && ` · ${t("inferredNote")}`}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <p className="text-xs text-muted-foreground">{t("appendHint")}</p>
            {preview.unsupported_capture_count > 0 && (
              <p className="text-xs text-muted-foreground">
                {t("unsupportedCaptureBanner", {
                  n: preview.unsupported_capture_count,
                })}
              </p>
            )}
            {preview.broker_groups.length > 0 && (
              <BrokerSummary groups={preview.broker_groups} />
            )}
            {preview.valid_rows.length > 0 && (
              <PreviewTable rows={preview.valid_rows} />
            )}

            {preview.issue_rows.length > 0 && (
              <div className="space-y-2">
                <h3 className="text-sm font-medium text-destructive">
                  {t("issuesHeading")}
                </h3>
                <p className="text-xs text-muted-foreground">
                  {t("issuesCount", { n: preview.issue_rows.length })}
                </p>
                <IssueList rows={preview.issue_rows} />
              </div>
            )}

            <div className="flex flex-wrap justify-end gap-2">
              <Button
                variant="ghost"
                onClick={() => setPreview(null)}
                disabled={saving}
              >
                {t("cancelButton")}
              </Button>
              <Button
                variant="outline"
                onClick={onReplaceClick}
                disabled={saving || preview.valid_rows.length === 0}
              >
                {t("replaceAllButton")}
              </Button>
              <Button
                onClick={onAppendClick}
                disabled={saving || preview.valid_rows.length === 0}
              >
                {saving ? t("saving") : t("appendButton")}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {mode !== "onboarding" && (
        <Card>
          <CardHeader>
            <CardTitle>{t("currentHeading")}</CardTitle>
            <CardAction>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  render={<Link href="/holdings/edit" />}
                >
                  {t("editHoldings")}
                </Button>
                {holdings.length > 0 && (
                  <Button variant="outline" size="sm" onClick={() => void onExport()}>
                    {t("exportButton")}
                  </Button>
                )}
              </div>
            </CardAction>
          </CardHeader>
          <CardContent>
            <HoldingsTable holdings={holdings} />
          </CardContent>
        </Card>
      )}

      <AlertDialog open={issuesConfirmOpen} onOpenChange={setIssuesConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("confirmTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {preview ? t("confirmBody", { n: preview.issue_rows.length }) : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={saving}>
              {t("confirmKeep")}
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => void doSave("append")}
              disabled={saving}
            >
              {saving ? t("saving") : t("confirmDiscard")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={replaceConfirmOpen} onOpenChange={setReplaceConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("replaceConfirmTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("replaceConfirmBody", { n: preview?.issue_rows.length ?? 0 })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={saving}>
              {t("cancelButton")}
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => void doSave("replace")}
              disabled={saving}
            >
              {saving ? t("saving") : t("replaceConfirmAction")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
