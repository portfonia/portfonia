"use client";

import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import {
  ApiError,
  createHolding,
  updateHolding,
  type HoldingOut,
  type HoldingPatch,
  type ParsedRow,
} from "@/lib/api";
import { isNextRedirectError } from "@/lib/next-redirect-error";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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

const CURRENCIES = [
  "USD",
  "CNY",
  "CNH",
  "HKD",
  "GBP",
  "EUR",
  "JPY",
  "SGD",
  "AUD",
  "CAD",
  "CHF",
  "KRW",
  "TWD",
  "MOP",
  "NZD",
] as const;

const ASSET_TYPES = ["stock", "etf", "fund", "cash", "wmf", "other"] as const;
const MARKETS = ["US", "HK", "A-Share", "UK", "Europe", "Japan", "Korea", "Other"] as const;

type FormState = {
  name: string;
  ticker: string;
  fund_code: string;
  currency: string;
  shares: string;
  avg_cost: string;
  current_value: string;
  pricing_mode: "auto" | "manual";
  asset_type: string;
  market: string;
  broker: string;
  account: string;
  portfolio: string;
  notes: string;
};

function emptyToNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function parseNum(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

function fromHolding(h: HoldingOut): FormState {
  return {
    name: h.name,
    ticker: h.ticker ?? "",
    fund_code: h.fund_code ?? "",
    currency: h.currency,
    shares: h.shares ?? "",
    avg_cost: h.avg_cost ?? "",
    current_value: h.current_value ?? "",
    pricing_mode: h.pricing_mode === "manual" ? "manual" : "auto",
    asset_type: h.asset_type ?? "",
    market: h.market ?? "",
    broker: h.broker ?? "",
    account: h.account ?? "",
    portfolio: h.portfolio ?? "",
    notes: h.notes ?? "",
  };
}

const EMPTY: FormState = {
  name: "",
  ticker: "",
  fund_code: "",
  currency: "USD",
  shares: "",
  avg_cost: "",
  current_value: "",
  pricing_mode: "auto",
  asset_type: "stock",
  market: "",
  broker: "",
  account: "",
  portfolio: "",
  notes: "",
};

function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-sm font-medium">{label}</span>
      {children}
    </label>
  );
}

export function HoldingForm({
  initial,
}: {
  initial?: HoldingOut;
}) {
  const t = useTranslations("holdings");
  const router = useRouter();
  const baseline = useMemo(() => (initial ? fromHolding(initial) : EMPTY), [initial]);
  const [form, setForm] = useState<FormState>(baseline);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [discardOpen, setDiscardOpen] = useState(false);

  const dirty = JSON.stringify(form) !== JSON.stringify(baseline);
  const isCashWmf = form.asset_type === "cash" || form.asset_type === "wmf";

  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => {
      const next = { ...prev, [key]: value };
      if (key === "asset_type" && (value === "cash" || value === "wmf")) {
        next.ticker = "";
        next.fund_code = "";
        next.shares = "";
        next.avg_cost = "";
        next.pricing_mode = "manual";
        if (!next.market) next.market = "Other";
      }
      return next;
    });
  }

  function goBack() {
    router.push("/holdings/edit");
  }

  function onBackClick() {
    if (dirty) {
      setDiscardOpen(true);
      return;
    }
    goBack();
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (saving) return;
    setSaving(true);
    setError(null);
    const fields: HoldingPatch = {
      name: form.name.trim(),
      ticker: isCashWmf ? null : emptyToNull(form.ticker),
      fund_code: isCashWmf ? null : emptyToNull(form.fund_code),
      currency: form.currency,
      shares: isCashWmf ? null : parseNum(form.shares),
      avg_cost: isCashWmf ? null : parseNum(form.avg_cost),
      current_value: parseNum(form.current_value),
      pricing_mode: isCashWmf ? "manual" : form.pricing_mode,
      asset_type: emptyToNull(form.asset_type) as HoldingPatch["asset_type"],
      market: (emptyToNull(form.market) as ParsedRow["market"]) ?? null,
      broker: emptyToNull(form.broker),
      account: emptyToNull(form.account),
      portfolio: emptyToNull(form.portfolio),
      notes: emptyToNull(form.notes),
    };
    try {
      if (initial) {
        await updateHolding(initial.id, fields);
      } else {
        await createHolding({ ...fields, issues: [], confidence: 1 } as ParsedRow);
      }
      router.push("/holdings/edit");
    } catch (err) {
      if (isNextRedirectError(err)) throw err;
      setError(
        `${initial ? t("errorUpdateFailed") : t("errorCreateFailed")}: ${
          err instanceof ApiError ? err.message : String(err)
        }`,
      );
    } finally {
      setSaving(false);
    }
  }

  const selectClass =
    "h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm";

  return (
    <>
      <header className="mb-8">
        <h1 className="font-heading text-2xl font-semibold">
          {initial ? t("editPageTitleForm") : t("newPageTitle")}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {initial ? t("editPageSubtitleForm") : t("newPageSubtitle")}
        </p>
      </header>
      {error && (
        <div className="mb-6 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}
      <form onSubmit={(e) => void onSubmit(e)} className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("fieldName")}>
            <Input
              required
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
            />
          </Field>
          <Field label={t("fieldAssetType")}>
            <select
              className={selectClass}
              value={form.asset_type}
              onChange={(e) => set("asset_type", e.target.value)}
            >
              {ASSET_TYPES.map((a) => (
                <option key={a} value={a}>
                  {t(
                    a === "stock"
                      ? "assetTypeStock"
                      : a === "etf"
                        ? "assetTypeEtf"
                        : a === "fund"
                          ? "assetTypeFund"
                          : a === "cash"
                            ? "assetTypeCash"
                            : a === "wmf"
                              ? "assetTypeWmf"
                              : "assetTypeOther",
                  )}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("fieldTicker")}>
            <Input
              value={form.ticker}
              disabled={isCashWmf}
              onChange={(e) => set("ticker", e.target.value)}
            />
          </Field>
          <Field label={t("fieldFundCode")}>
            <Input
              value={form.fund_code}
              disabled={isCashWmf}
              onChange={(e) => set("fund_code", e.target.value)}
            />
          </Field>
          <Field label={t("fieldCurrency")}>
            <select
              className={selectClass}
              value={form.currency}
              onChange={(e) => set("currency", e.target.value)}
            >
              {CURRENCIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("fieldMarket")}>
            <select
              className={selectClass}
              value={form.market}
              onChange={(e) => set("market", e.target.value)}
            >
              <option value="">{t("marketOther")}</option>
              {MARKETS.map((m) => (
                <option key={m} value={m}>
                  {m === "US"
                    ? t("marketUS")
                    : m === "HK"
                      ? t("marketHK")
                      : m === "A-Share"
                        ? t("marketAShare")
                        : m === "UK"
                          ? t("marketUK")
                          : m === "Europe"
                            ? t("marketEurope")
                            : m === "Japan"
                              ? t("marketJapan")
                              : m === "Korea"
                                ? t("marketKorea")
                                : t("marketOther")}
                </option>
              ))}
            </select>
          </Field>
          {!isCashWmf && (
            <>
              <Field label={t("fieldShares")}>
                <Input
                  type="number"
                  min={0}
                  step="any"
                  value={form.shares}
                  onChange={(e) => set("shares", e.target.value)}
                />
              </Field>
              <Field label={t("fieldAvgCost")}>
                <Input
                  type="number"
                  min={0}
                  step="any"
                  value={form.avg_cost}
                  onChange={(e) => set("avg_cost", e.target.value)}
                />
              </Field>
            </>
          )}
          <Field label={t("fieldCurrentValue")}>
            <Input
              type="number"
              min={0}
              step="any"
              required={isCashWmf}
              value={form.current_value}
              onChange={(e) => set("current_value", e.target.value)}
            />
          </Field>
          <Field label={t("fieldPricingMode")}>
            <select
              className={selectClass}
              value={isCashWmf ? "manual" : form.pricing_mode}
              disabled={isCashWmf}
              onChange={(e) =>
                set("pricing_mode", e.target.value === "manual" ? "manual" : "auto")
              }
            >
              <option value="auto">{t("pricingAuto")}</option>
              <option value="manual">{t("pricingManual")}</option>
            </select>
          </Field>
          <Field label={t("fieldBroker")}>
            <Input value={form.broker} onChange={(e) => set("broker", e.target.value)} />
          </Field>
          <Field label={t("fieldAccount")}>
            <Input value={form.account} onChange={(e) => set("account", e.target.value)} />
          </Field>
          <Field label={t("fieldPortfolio")}>
            <Input
              value={form.portfolio}
              onChange={(e) => set("portfolio", e.target.value)}
            />
          </Field>
          <Field label={t("fieldNotes")}>
            <Input value={form.notes} onChange={(e) => set("notes", e.target.value)} />
          </Field>
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="ghost" onClick={onBackClick} disabled={saving}>
            {t("backToEdit")}
          </Button>
          <Button type="submit" disabled={saving}>
            {saving ? t("saving") : t("saveHolding")}
          </Button>
        </div>
      </form>

      <AlertDialog open={discardOpen} onOpenChange={setDiscardOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("discardTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("discardBody")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("stay")}</AlertDialogCancel>
            <AlertDialogAction variant="destructive" onClick={goBack}>
              {t("discardConfirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
