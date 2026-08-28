"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import {
  ApiError,
  confirmHoldings,
  exportHoldings,
  uploadHoldings,
  type HoldingOut,
  type UploadPreview,
} from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { TEMPLATE_MARKDOWN, downloadFile } from "@/lib/template";
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
import { BrokerSummary, IssueList, PreviewTable } from "./preview";

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
  // Errors set by upload/save handlers below (translated at the moment the
  // handler runs). The page-load error path is kept separate as a plain
  // boolean (see `displayError`) so it stays reactive to a later locale
  // switch instead of freezing whatever language was active at mount.
  const [error, setError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const displayError = error ?? (initialLoadError ? t("errorLoadFailed") : null);

  useEffect(() => {
    if (!uploading) return;
    const id = setInterval(() => setUploadSeconds((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [uploading]);

  // The original threshold logic (which sentence shows at which elapsed
  // time) is UI behavior, not translatable content, so it stays here — only
  // the four resulting strings come from the catalog.
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
      // A 401 here can be the idle-logout Server Action's own redirect()
      // throw (issue #235/#240) — that must propagate, not become an
      // upload-failed error message.
      if (isNextRedirectError(err)) throw err;
      setError(
        `${t("errorUploadFailed")}: ${err instanceof ApiError ? err.message : String(err)}`,
      );
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function doSave() {
    if (!preview) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await confirmHoldings(preview.valid_rows);
      if (mode === "onboarding") {
        // Ring 1-Onboarding.md §2.3 — the other Save destination, alongside
        // the questionnaire's. Preview is memory-only by design, so nothing
        // else needs clearing on the way out.
        router.push("/welcome");
        return;
      }
      setHoldings(saved);
      setPreview(null);
    } catch (err) {
      // A 401 here can be the idle-logout Server Action's own redirect()
      // throw (issue #235/#240) — that must propagate, not become a
      // save-failed error message.
      if (isNextRedirectError(err)) throw err;
      setError(
        `${t("errorSaveFailed")}: ${err instanceof ApiError ? err.message : String(err)}`,
      );
    } finally {
      setSaving(false);
      setConfirmOpen(false);
    }
  }

  function onSaveClick() {
    if (!preview) return;
    if (preview.issue_rows.length > 0) {
      setConfirmOpen(true);
    } else {
      void doSave();
    }
  }

  async function onExport() {
    try {
      downloadFile(await exportHoldings(), "holdings.md");
    } catch (err) {
      // A 401 here can be the idle-logout Server Action's own redirect()
      // throw (issue #235/#240) — that must propagate, not become an
      // export error message.
      if (isNextRedirectError(err)) throw err;
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  const hasInferred =
    preview?.valid_rows.some(
      (r) => r.issues.length > 0 || r.confidence < 0.7,
    ) ?? false;

  return (
    <>
      {displayError && (
        <div className="mb-6 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {displayError}
        </div>
      )}

      {/* Import */}
      <Card className="mb-6">
        <CardHeader>
          <CardTitle>{t("uploadHeading")}</CardTitle>
          <CardDescription>{t("uploadHint")}</CardDescription>
          {mode !== "onboarding" && (
            <CardAction>
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  downloadFile(TEMPLATE_MARKDOWN, "holdings-template.md")
                }
              >
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

      {/* Preview */}
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
            <p className="text-xs text-muted-foreground">{t("replaceWarning")}</p>
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

            <div className="flex justify-end gap-2">
              <Button
                variant="ghost"
                onClick={() => setPreview(null)}
                disabled={saving}
              >
                {t("cancelButton")}
              </Button>
              <Button
                onClick={onSaveClick}
                disabled={saving || preview.valid_rows.length === 0}
              >
                {saving ? t("saving") : t("saveButton")}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Current holdings — hidden in onboarding mode (Ring 1-Onboarding.md
          §2.3): nothing is committed yet on a brand-new signup, and Export
          lives inside this card so hiding it takes Export with it. */}
      {mode !== "onboarding" && (
        <Card>
          <CardHeader>
            <CardTitle>{t("currentHeading")}</CardTitle>
            {holdings.length > 0 && (
              <CardAction>
                <Button variant="outline" size="sm" onClick={onExport}>
                  {t("exportButton")}
                </Button>
              </CardAction>
            )}
          </CardHeader>
          <CardContent>
            <HoldingsTable holdings={holdings} />
          </CardContent>
        </Card>
      )}

      {/* Last-chance discard confirmation */}
      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
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
              onClick={() => void doSave()}
              disabled={saving}
            >
              {saving ? t("saving") : t("confirmDiscard")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
