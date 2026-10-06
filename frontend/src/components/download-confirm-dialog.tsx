"use client";

import { useTranslations } from "next-intl";

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
import { downloadFile } from "@/lib/template";

// issue #679: every download is fetched first, then confirmed. The file
// name for server-generated files only exists after the fetch
// (Content-Disposition), and at that point blob.size is exact.
export interface PendingDownload {
  blob: Blob;
  filename: string;
  description: string;
}

// Every file this dialog handles is far below 1 MB, so there is no MB branch.
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

export function DownloadConfirmDialog({
  pending,
  onClose,
}: {
  pending: PendingDownload | null;
  onClose: () => void;
}) {
  const t = useTranslations("downloadConfirm");

  function confirm() {
    if (pending) downloadFile(pending.blob, pending.filename);
    onClose();
  }

  return (
    <AlertDialog
      open={pending !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t("title")}</AlertDialogTitle>
          <AlertDialogDescription>{pending?.description}</AlertDialogDescription>
        </AlertDialogHeader>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-muted-foreground">{t("fileName")}</dt>
          <dd className="min-w-0 break-all">{pending?.filename}</dd>
          <dt className="text-muted-foreground">{t("fileSize")}</dt>
          <dd>{pending ? formatFileSize(pending.blob.size) : null}</dd>
        </dl>
        <AlertDialogFooter>
          <AlertDialogCancel>{t("cancel")}</AlertDialogCancel>
          <AlertDialogAction onClick={confirm}>{t("confirm")}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
