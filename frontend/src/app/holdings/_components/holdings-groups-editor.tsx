"use client";

import { useId, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { ApiError, updateHolding, type HoldingOut, type HoldingPatch } from "@/lib/api";
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

function cell(value: string | null | undefined): string {
  return value ?? "—";
}

// Free-text input offering the distinct group/account values already in
// use as <datalist> suggestions, while still accepting arbitrary new text
// (issue #430 requirement 6) — no dedicated combobox component exists in
// this repo, and a native <input list> needs no new dependency for this.
// Uncontrolled + re-keyed on the committed value, same pattern as
// holdings-editor.tsx's NumberCell.
function TextCell({
  value,
  disabled,
  listId,
  onCommit,
}: {
  value: string | null;
  disabled: boolean;
  listId: string;
  onCommit: (raw: string) => void;
}) {
  return (
    <input
      key={value ?? ""}
      type="text"
      list={listId}
      defaultValue={value ?? ""}
      disabled={disabled}
      className="h-8 w-full min-w-32 rounded-md border border-input bg-transparent px-2 text-sm disabled:opacity-50"
      onBlur={(e) => {
        const raw = e.target.value.trim();
        if (raw === (value ?? "")) return;
        onCommit(raw);
      }}
    />
  );
}

function distinctValues(holdings: HoldingOut[], field: "portfolio" | "account"): string[] {
  const values = new Set<string>();
  for (const h of holdings) {
    const v = h[field];
    if (v) values.add(v);
  }
  return [...values].sort((a, b) => a.localeCompare(b));
}

export function HoldingsGroupsEditor({
  initialHoldings,
  initialLoadError = false,
}: {
  initialHoldings: HoldingOut[];
  initialLoadError?: boolean;
}) {
  const t = useTranslations("holdings");
  const [holdings, setHoldings] = useState<HoldingOut[]>(initialHoldings);
  const [error, setError] = useState<string | null>(null);
  const [savingCells, setSavingCells] = useState<Set<string>>(new Set());
  const groupListId = useId();
  const accountListId = useId();

  const displayError = error ?? (initialLoadError ? t("errorLoadFailed") : null);

  async function patchField(id: string, field: "portfolio" | "account", raw: string) {
    const previous = holdings.find((h) => h.id === id)?.[field] ?? null;
    const next = raw === "" ? null : raw;
    const cellKey = `${id}:${field}`;
    setHoldings((prev) => prev.map((h) => (h.id === id ? { ...h, [field]: next } : h)));
    setSavingCells((prev) => new Set(prev).add(cellKey));
    setError(null);
    const patch: HoldingPatch = { [field]: next };
    try {
      await updateHolding(id, patch);
    } catch (err) {
      if (isNextRedirectError(err)) throw err;
      setHoldings((prev) => prev.map((h) => (h.id === id ? { ...h, [field]: previous } : h)));
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

  const groupOptions = distinctValues(holdings, "portfolio");
  const accountOptions = distinctValues(holdings, "account");

  return (
    <>
      <header className="mb-8">
        <h1 className="font-heading text-2xl font-semibold">{t("groupsPageTitle")}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{t("groupsPageSubtitle")}</p>
      </header>

      {displayError && (
        <div className="mb-6 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {displayError}
        </div>
      )}

      {holdings.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("emptyEditState")}</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("colName")}</TableHead>
              <TableHead>{t("colTicker")}</TableHead>
              <TableHead>{t("colBroker")}</TableHead>
              <TableHead>{t("colCurrency")}</TableHead>
              <TableHead className="text-right">{t("colCurrentValue")}</TableHead>
              <TableHead>{t("colGroup")}</TableHead>
              <TableHead>{t("colAccount")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {holdings.map((h) => (
              <TableRow key={h.id}>
                <TableCell className="font-medium">{h.name}</TableCell>
                <TableCell>{cell(h.ticker ?? h.fund_code)}</TableCell>
                <TableCell>{cell(h.broker)}</TableCell>
                <TableCell>{h.currency}</TableCell>
                <TableCell className="text-right tabular-nums">{cell(h.current_value)}</TableCell>
                <TableCell>
                  <TextCell
                    value={h.portfolio}
                    disabled={savingCells.has(`${h.id}:portfolio`)}
                    listId={groupListId}
                    onCommit={(raw) => void patchField(h.id, "portfolio", raw)}
                  />
                </TableCell>
                <TableCell>
                  <TextCell
                    value={h.account}
                    disabled={savingCells.has(`${h.id}:account`)}
                    listId={accountListId}
                    onCommit={(raw) => void patchField(h.id, "account", raw)}
                  />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <datalist id={groupListId}>
        {groupOptions.map((v) => (
          <option key={v} value={v} />
        ))}
      </datalist>
      <datalist id={accountListId}>
        {accountOptions.map((v) => (
          <option key={v} value={v} />
        ))}
      </datalist>

      <div className="mt-6">
        <Button variant="ghost" render={<Link href="/holdings/edit" />}>
          {t("backToHoldingsEdit")}
        </Button>
      </div>
    </>
  );
}
