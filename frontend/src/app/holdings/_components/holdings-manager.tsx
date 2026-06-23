"use client";

import { useEffect, useRef, useState } from "react";

import { messages } from "@/lib/messages";
import {
  ApiError,
  confirmHoldings,
  exportHoldings,
  uploadHoldings,
  type HoldingOut,
  type UploadPreview,
} from "@/lib/api";
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
import { IssueList, PreviewTable } from "./preview";

const m = messages.holdings;

export function HoldingsManager({
  initialHoldings,
  initialError = null,
}: {
  initialHoldings: HoldingOut[];
  initialError?: string | null;
}) {
  const [holdings, setHoldings] = useState<HoldingOut[]>(initialHoldings);
  const [preview, setPreview] = useState<UploadPreview | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadSeconds, setUploadSeconds] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(initialError);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!uploading) return;
    setUploadSeconds(0);
    const id = setInterval(() => setUploadSeconds((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [uploading]);

  async function onFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setError(null);
    setUploading(true);
    setPreview(null);
    try {
      setPreview(await uploadHoldings(file));
    } catch (err) {
      setError(
        `${m.errorUploadFailed}: ${err instanceof ApiError ? err.message : String(err)}`,
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
      setHoldings(await confirmHoldings(preview.valid_rows));
      setPreview(null);
    } catch (err) {
      setError(
        `${m.errorSaveFailed}: ${err instanceof ApiError ? err.message : String(err)}`,
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
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  const hasInferred =
    preview?.valid_rows.some((r) => r.issues.length > 0 || r.confidence < 0.7) ??
    false;

  return (
    <>
      {error && (
        <div className="mb-6 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {/* Import */}
      <Card className="mb-6">
        <CardHeader>
          <CardTitle>{m.uploadHeading}</CardTitle>
          <CardDescription>{m.uploadHint}</CardDescription>
          <CardAction>
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                downloadFile(TEMPLATE_MARKDOWN, "holdings-template.md")
              }
            >
              {m.downloadTemplate}
            </Button>
          </CardAction>
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
              {uploading ? m.uploading : m.chooseFile}
            </Button>
            {uploading && (
              <span className="text-sm text-muted-foreground">
                {m.uploadingProgress(uploadSeconds)}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Preview */}
      {preview && (
        <Card className="mb-6">
          <CardHeader>
            <CardTitle>{m.previewHeading}</CardTitle>
            <CardDescription>
              {m.previewValidCount(preview.valid_rows.length)}
              {hasInferred && ` · ${m.inferredNote}`}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <p className="text-xs text-muted-foreground">{m.replaceWarning}</p>
            {preview.valid_rows.length > 0 && (
              <PreviewTable rows={preview.valid_rows} />
            )}

            {preview.issue_rows.length > 0 && (
              <div className="space-y-2">
                <h3 className="text-sm font-medium text-destructive">
                  {m.issuesHeading}
                </h3>
                <p className="text-xs text-muted-foreground">
                  {m.issuesCount(preview.issue_rows.length)}
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
                {m.cancelButton}
              </Button>
              <Button
                onClick={onSaveClick}
                disabled={saving || preview.valid_rows.length === 0}
              >
                {saving ? m.saving : m.saveButton}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Current holdings */}
      <Card>
        <CardHeader>
          <CardTitle>{m.currentHeading}</CardTitle>
          {holdings.length > 0 && (
            <CardAction>
              <Button variant="outline" size="sm" onClick={onExport}>
                {m.exportButton}
              </Button>
            </CardAction>
          )}
        </CardHeader>
        <CardContent>
          <HoldingsTable holdings={holdings} />
        </CardContent>
      </Card>

      {/* Last-chance discard confirmation */}
      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{m.confirmTitle}</AlertDialogTitle>
            <AlertDialogDescription>
              {preview ? m.confirmBody(preview.issue_rows.length) : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={saving}>
              {m.confirmKeep}
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => void doSave()}
              disabled={saving}
            >
              {saving ? m.saving : m.confirmDiscard}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
