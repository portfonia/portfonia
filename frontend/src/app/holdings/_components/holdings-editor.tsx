"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { ChevronDown, ChevronUp, GripVertical } from "lucide-react";

import {
  ApiError,
  deleteHolding,
  reorderHoldings,
  updateHolding,
  type HoldingOut,
  type HoldingPatch,
} from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { Button } from "@/components/ui/button";
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

function isCashWmf(h: HoldingOut): boolean {
  return h.asset_type === "cash" || h.asset_type === "wmf";
}

// Empty-string-safe parse mirroring holding-form.tsx's parseNum: this file
// has its own copy rather than a shared import since both are three lines
// and the two forms (whole-record vs. one inline cell) are unlikely to stay
// in lockstep.
function parseNum(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

// A sortable column's field and, for "broker", its tie-break secondary key
// (issue #319 item 12 — broker's secondary sort is always ticker).
type SortField = "ticker" | "currency" | "broker";
type SortDirection = "asc" | "desc";

function compareStr(a: string | null | undefined, b: string | null | undefined): number {
  return (a ?? "").localeCompare(b ?? "");
}

function sortedIds(list: HoldingOut[], field: SortField, direction: SortDirection): string[] {
  const sign = direction === "asc" ? 1 : -1;
  const sorted = [...list].sort((a, b) => {
    let primary: number;
    if (field === "ticker") {
      primary = compareStr(a.ticker ?? a.fund_code, b.ticker ?? b.fund_code);
    } else if (field === "currency") {
      primary = compareStr(a.currency, b.currency);
    } else {
      primary = compareStr(a.broker, b.broker);
      if (primary === 0) primary = compareStr(a.ticker ?? a.fund_code, b.ticker ?? b.fund_code);
    }
    return sign * primary;
  });
  return sorted.map((h) => h.id);
}

function NumberCell({
  value,
  disabled,
  onCommit,
}: {
  value: string | null;
  disabled: boolean;
  onCommit: (raw: string) => void;
}) {
  return (
    <input
      // Resets the input's own DOM state whenever the committed value
      // changes underneath it (a successful save, or a rollback) — an
      // uncontrolled input re-keyed on its source of truth, rather than a
      // controlled one fighting the user's keystrokes on every render.
      key={value ?? ""}
      type="number"
      step="any"
      defaultValue={value ?? ""}
      disabled={disabled}
      className="h-8 w-24 rounded-md border border-input bg-transparent px-2 text-right text-sm tabular-nums disabled:opacity-50"
      onClick={(e) => e.stopPropagation()}
      onBlur={(e) => {
        const raw = e.target.value;
        if (raw === (value ?? "")) return;
        onCommit(raw);
      }}
    />
  );
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
  // A set, not a single key (PR #321 review round 1): tabbing between two
  // cells before the first PATCH resolves must track both as independently
  // in-flight, not have the second edit's key overwrite the first's and
  // leave its input showing enabled while its request is still pending.
  const [savingCells, setSavingCells] = useState<Set<string>>(new Set());
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<HoldingOut | null>(null);
  const dragFrom = useRef<number | null>(null);

  const displayError = error ?? (initialLoadError ? t("errorLoadFailed") : null);

  async function applyReorder(ids: string[]) {
    // Mutually exclusive with an in-flight field save (PR #321 review
    // round 2): applyReorder's success handler replaces the whole
    // `holdings` array with the reorder response, which can overwrite a
    // field PATCH that committed after the reorder request was sent but
    // before its response arrived — a cross-endpoint last-write-wins race
    // the round-1 per-field rollback does not cover.
    if (reordering || savingCells.size > 0) return;
    const previous = holdings;
    const next = ids.map((id) => holdings.find((h) => h.id === id)).filter((h) => h != null);
    setHoldings(next);
    setReordering(true);
    setError(null);
    try {
      const saved = await reorderHoldings(ids);
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

  async function onDropRow(to: number) {
    const from = dragFrom.current;
    dragFrom.current = null;
    if (from == null || from === to || reordering) return;
    await applyReorder(moveItem(holdings, from, to).map((h) => h.id));
  }

  // Item 12: header-click sort reuses PATCH /holdings/reorder — the exact
  // same call and optimistic/rollback logic as drag reorder above — so the
  // displayed order and persisted `position` can never diverge between the
  // two entry points.
  async function onSortClick(field: SortField, direction: SortDirection) {
    await applyReorder(sortedIds(holdings, field, direction));
  }

  async function patchField(
    id: string,
    patch: HoldingPatch,
    field: "shares" | "avg_cost" | "current_value" | "pricing_mode",
    previousValue: string | null,
    optimisticValue: string,
  ) {
    // Mutually exclusive with an in-flight reorder — see applyReorder's
    // matching guard above for why (PR #321 review round 2). The inline
    // controls are also visually disabled while reordering, so this is
    // belt-and-suspenders against a commit that started just before that.
    if (reordering) return;
    const cellKey = `${id}:${field}`;
    // Optimistic update mirrors drag reorder's previous-state rollback
    // pattern (design decision 4-5): NumberCell is an uncontrolled input
    // keyed on this same value, so writing it here also remounts the input
    // to the reverted text on failure — without this, a failed PATCH would
    // have nothing to roll back from and the stale typed value would stick.
    setHoldings((prev) =>
      prev.map((h) => (h.id === id ? { ...h, [field]: optimisticValue } : h)),
    );
    setSavingCells((prev) => new Set(prev).add(cellKey));
    setError(null);
    try {
      const saved = await updateHolding(id, patch);
      // Merge only this field, plus the metadata fields a pricing_mode
      // change can recompute server-side (capture_supported/asset_class/
      // market never depend on shares/avg_cost/current_value, only on
      // ticker/currency/asset_type/pricing_mode, so refreshing them from
      // any inline-edit response is always safe) — not the whole row
      // (PR #321 review round 3): two fields on the same row can be
      // in-flight at once (savingCells is a Set precisely because tabbing
      // between cells doesn't wait for the first response), and replacing
      // the entire row from one PATCH's response could clobber a sibling
      // field's already-applied optimistic update or successful save.
      setHoldings((prev) =>
        prev.map((h) =>
          h.id === id
            ? {
                ...h,
                [field]: saved[field],
                capture_supported: saved.capture_supported,
                asset_class: saved.asset_class,
                market: saved.market,
                last_manual_update: saved.last_manual_update,
              }
            : h,
        ),
      );
    } catch (err) {
      if (isNextRedirectError(err)) throw err;
      // Roll back only this field, not the whole row (PR #321 review
      // round 1): a second field on the same row may have optimistically
      // applied — or already saved — in the meantime, and reverting to a
      // whole-row snapshot captured before this edit would clobber that
      // unrelated, possibly-already-successful change.
      setHoldings((prev) =>
        prev.map((h) => (h.id === id ? { ...h, [field]: previousValue } : h)),
      );
      setError(
        `${t("errorUpdateFailed")}: ${err instanceof ApiError ? err.message : String(err)}`,
      );
    } finally {
      setSavingCells((prev) => {
        const next = new Set(prev);
        next.delete(cellKey);
        return next;
      });
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

  // A function returning JSX (called inline below), not a component
  // reference — a `<SortHeader .../>` tag defined inside the render body
  // would be re-created every render and reset its (nonexistent, but
  // react-hooks/static-components still flags it) internal state.
  function sortHeader(field: SortField, label: string) {
    return (
      <TableHead>
        <span className="inline-flex items-center gap-1">
          {label}
          <span className="inline-flex flex-col">
            <button
              type="button"
              aria-label={`${t("sortAscending")}: ${label}`}
              className="text-muted-foreground hover:text-foreground disabled:opacity-50"
              disabled={reordering || savingCells.size > 0}
              onClick={() => void onSortClick(field, "asc")}
            >
              <ChevronUp className="size-3" aria-hidden="true" />
            </button>
            <button
              type="button"
              aria-label={`${t("sortDescending")}: ${label}`}
              className="text-muted-foreground hover:text-foreground disabled:opacity-50"
              disabled={reordering || savingCells.size > 0}
              onClick={() => void onSortClick(field, "desc")}
            >
              <ChevronDown className="size-3" aria-hidden="true" />
            </button>
          </span>
        </span>
      </TableHead>
    );
  }

  return (
    <>
      <header className="mb-8">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h1 className="font-heading text-2xl font-semibold">{t("editPageTitle")}</h1>
          <Link href="/portfolio" className="text-sm text-muted-foreground underline underline-offset-4">
            {t("viewPortfolioLink")}
          </Link>
        </div>
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
              {sortHeader("ticker", t("colTicker"))}
              {sortHeader("currency", t("colCurrency"))}
              <TableHead className="text-right">{t("colShares")}</TableHead>
              <TableHead className="text-right">{t("colAvgCost")}</TableHead>
              <TableHead className="text-right">{t("colCurrentValue")}</TableHead>
              <TableHead>{t("colPricingMode")}</TableHead>
              {sortHeader("broker", t("colBroker"))}
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {holdings.map((h, i) => {
              const cashWmf = isCashWmf(h);
              return (
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
                  onClick={(e) => {
                    // Belt-and-suspenders alongside each editable control's
                    // own stopPropagation (item 4-5): a native <select>'s
                    // click sequence bubbles through its <option> children
                    // in a way that doesn't reliably stop here otherwise.
                    if ((e.target as HTMLElement).closest("input, select, button")) return;
                    router.push(`/holdings/${h.id}`);
                  }}
                >
                  <TableCell>
                    <button
                      type="button"
                      aria-label={t("dragHandle")}
                      draggable
                      disabled={savingCells.size > 0}
                      className="cursor-grab text-muted-foreground active:cursor-grabbing disabled:cursor-not-allowed disabled:opacity-50"
                      onClick={(e) => e.stopPropagation()}
                      onDragStart={() => {
                        // Mutually exclusive with an in-flight field save
                        // (PR #321 review round 2) — same reasoning as
                        // applyReorder's own guard. Belt-and-suspenders
                        // alongside `disabled` above.
                        if (savingCells.size > 0) return;
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
                  <TableCell className="text-right tabular-nums">
                    {cashWmf ? (
                      cell(h.shares)
                    ) : (
                      <NumberCell
                        value={h.shares}
                        disabled={reordering || savingCells.has(`${h.id}:shares`)}
                        onCommit={(raw) =>
                          void patchField(
                            h.id,
                            { shares: parseNum(raw) },
                            "shares",
                            h.shares,
                            raw,
                          )
                        }
                      />
                    )}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {cashWmf ? (
                      cell(h.avg_cost)
                    ) : (
                      <NumberCell
                        value={h.avg_cost}
                        disabled={reordering || savingCells.has(`${h.id}:avg_cost`)}
                        onCommit={(raw) =>
                          void patchField(
                            h.id,
                            { avg_cost: parseNum(raw) },
                            "avg_cost",
                            h.avg_cost,
                            raw,
                          )
                        }
                      />
                    )}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {cashWmf ? (
                      <NumberCell
                        value={h.current_value}
                        disabled={reordering || savingCells.has(`${h.id}:current_value`)}
                        onCommit={(raw) =>
                          void patchField(
                            h.id,
                            { current_value: parseNum(raw) },
                            "current_value",
                            h.current_value,
                            raw,
                          )
                        }
                      />
                    ) : (
                      cell(h.current_value)
                    )}
                  </TableCell>
                  <TableCell>
                    <select
                      className="h-8 rounded-lg border border-input bg-transparent px-2 text-sm disabled:opacity-50"
                      value={h.pricing_mode === "manual" ? "manual" : "auto"}
                      disabled={cashWmf || reordering || savingCells.has(`${h.id}:pricing_mode`)}
                      onClick={(e) => e.stopPropagation()}
                      onChange={(e) => {
                        const previous = h.pricing_mode === "manual" ? "manual" : "auto";
                        const next = e.target.value === "manual" ? "manual" : "auto";
                        void patchField(
                          h.id,
                          { pricing_mode: next },
                          "pricing_mode",
                          previous,
                          next,
                        );
                      }}
                    >
                      <option value="auto">{t("pricingAuto")}</option>
                      <option value="manual">{t("pricingManual")}</option>
                    </select>
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
              );
            })}
          </TableBody>
        </Table>
      )}

      <div className="mt-6">
        <Button variant="ghost" render={<Link href="/holdings" />}>
          {t("backToHoldingsList")}
        </Button>
      </div>

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
