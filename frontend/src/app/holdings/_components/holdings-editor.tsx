"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { GripVertical } from "lucide-react";

import {
  ApiError,
  deleteHolding,
  reorderHoldings,
  type HoldingOut,
} from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
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

function cell(value: string | null | undefined): string {
  return value ?? "—";
}

function moveItem<T>(list: T[], from: number, to: number): T[] {
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

export function HoldingsEditor({
  initialHoldings,
  initialLoadError = false,
  onboardingIncomplete = false,
}: {
  initialHoldings: HoldingOut[];
  initialLoadError?: boolean;
  onboardingIncomplete?: boolean;
}) {
  const t = useTranslations("holdings");
  const router = useRouter();
  const [holdings, setHoldings] = useState<HoldingOut[]>(initialHoldings);
  const [error, setError] = useState<string | null>(null);
  const [reordering, setReordering] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<HoldingOut | null>(null);
  const dragFrom = useRef<number | null>(null);

  const displayError = error ?? (initialLoadError ? t("errorLoadFailed") : null);

  async function onDropRow(to: number) {
    const from = dragFrom.current;
    dragFrom.current = null;
    if (from == null || from === to || reordering) return;
    const previous = holdings;
    const next = moveItem(holdings, from, to);
    setHoldings(next);
    setReordering(true);
    setError(null);
    try {
      const saved = await reorderHoldings(next.map((h) => h.id));
      setHoldings(saved);
    } catch (err) {
      if (isNextRedirectError(err)) throw err;
      setHoldings(previous);
      setError(
        `${t("reorderFailed")}: ${err instanceof ApiError ? err.message : String(err)}`,
      );
    } finally {
      setReordering(false);
    }
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    const id = pendingDelete.id;
    setDeletingId(id);
    setError(null);
    try {
      await deleteHolding(id);
      setHoldings((prev) => prev.filter((h) => h.id !== id));
      setPendingDelete(null);
    } catch (err) {
      if (isNextRedirectError(err)) throw err;
      setError(
        `${t("errorDeleteFailed")}: ${err instanceof ApiError ? err.message : String(err)}`,
      );
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <>
      <header className="mb-8">
        <h1 className="font-heading text-2xl font-semibold">{t("editPageTitle")}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{t("editPageSubtitle")}</p>
      </header>

      {onboardingIncomplete && (
        <div className="mb-6 rounded-lg border border-amber-300/60 bg-amber-50 px-4 py-3 text-sm dark:border-amber-800 dark:bg-amber-950/30">
          {t("onboardingBanner")}{" "}
          <Link href="/holdings?onboarding=1" className="underline underline-offset-4">
            {t("onboardingBannerLink")}
          </Link>
        </div>
      )}

      {displayError && (
        <div className="mb-6 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {displayError}
        </div>
      )}

      <div className="mb-4 flex justify-end">
        <Button render={<Link href="/holdings/new" />}>{t("addHolding")}</Button>
      </div>

      {holdings.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("emptyEditState")}</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-8" />
              <TableHead>{t("colName")}</TableHead>
              <TableHead>{t("colTicker")}</TableHead>
              <TableHead>{t("colCurrency")}</TableHead>
              <TableHead className="text-right">{t("colShares")}</TableHead>
              <TableHead className="text-right">{t("colAvgCost")}</TableHead>
              <TableHead className="text-right">{t("colCurrentValue")}</TableHead>
              <TableHead>{t("colPricingMode")}</TableHead>
              <TableHead>{t("colBroker")}</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {holdings.map((h, i) => (
              <TableRow
                key={h.id}
                className="cursor-pointer"
                onDragOver={(e) => {
                  e.preventDefault();
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  void onDropRow(i);
                }}
                onClick={() => router.push(`/holdings/${h.id}`)}
              >
                <TableCell>
                  <button
                    type="button"
                    aria-label={t("dragHandle")}
                    draggable
                    className="cursor-grab text-muted-foreground active:cursor-grabbing"
                    onClick={(e) => e.stopPropagation()}
                    onDragStart={() => {
                      dragFrom.current = i;
                    }}
                    onDragEnd={() => {
                      dragFrom.current = null;
                    }}
                  >
                    <GripVertical className="size-4" aria-hidden="true" />
                  </button>
                </TableCell>
                <TableCell className="font-medium">{h.name}</TableCell>
                <TableCell>{cell(h.ticker ?? h.fund_code)}</TableCell>
                <TableCell>{h.currency}</TableCell>
                <TableCell className="text-right tabular-nums">{cell(h.shares)}</TableCell>
                <TableCell className="text-right tabular-nums">{cell(h.avg_cost)}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {cell(h.current_value)}
                </TableCell>
                <TableCell>
                  <Badge variant={h.pricing_mode === "auto" ? "secondary" : "outline"}>
                    {h.pricing_mode}
                  </Badge>
                </TableCell>
                <TableCell>{cell(h.broker)}</TableCell>
                <TableCell>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={deletingId === h.id}
                    onClick={(e) => {
                      e.stopPropagation();
                      setPendingDelete(h);
                    }}
                  >
                    {t("deleteButton")}
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <AlertDialog
        open={pendingDelete != null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("deleteConfirmTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("deleteConfirmBody")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deletingId != null}>
              {t("cancelButton")}
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => void confirmDelete()}
              disabled={deletingId != null}
            >
              {deletingId ? t("saving") : t("deleteConfirmAction")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
