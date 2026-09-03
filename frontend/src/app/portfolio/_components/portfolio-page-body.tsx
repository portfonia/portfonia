"use client";

import { useState, useTransition } from "react";
import { useTranslations } from "next-intl";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { getPortfolioSummary, type PortfolioSummary } from "@/lib/api";
import { BreakdownChart } from "./breakdown-chart";
import { CurrencySwitcher } from "./currency-switcher";
import { DEFAULT_BASE_CURRENCY, type BaseCurrency } from "./currencies";
import { NoLivePriceSection } from "./no-live-price-section";
import { PnlSummaryCard } from "./pnl-summary-card";
import {
  ACCOUNT_OTHER_KEY,
  formatMoney,
  GROUP_UNGROUPED_KEY,
  partitionHoldings,
} from "./portfolio-helpers";
import { PortfolioHoldingsTable } from "./portfolio-holdings-table";
import { PriceAsOfBanner } from "./price-as-of-banner";

// Closed set — mirrors backend/app/services/asset_class_config.py's
// VALID_ASSET_CLASSES (13 entries, DB-constrained). Safe to translate
// directly with no existence check, same as reportScheduleOptions elsewhere
// in this catalog.
function assetClassLabel(t: ReturnType<typeof useTranslations<"portfolio">>, code: string): string {
  return t(`assetClasses.${code}`);
}

export function PortfolioPageBody({
  initialSummary,
  initialLoadError,
}: {
  initialSummary: PortfolioSummary | null;
  initialLoadError: boolean;
}) {
  const t = useTranslations("portfolio");
  const [summary, setSummary] = useState(initialSummary);
  const [currency, setCurrency] = useState<BaseCurrency>(
    (initialSummary?.base_currency as BaseCurrency | undefined) ?? DEFAULT_BASE_CURRENCY,
  );
  const [loadError, setLoadError] = useState(initialLoadError);
  const [isPending, startTransition] = useTransition();

  if (loadError && !summary) {
    return <p className="text-sm text-destructive">{t("loadError")}</p>;
  }
  if (!summary) {
    return null;
  }

  const handleCurrencyChange = (next: BaseCurrency) => {
    setCurrency(next);
    // Grok review round 1 (PR #322): the previous version fired the fetch
    // from inside a synchronous startTransition callback (`void promise.then
    // (...)`), so isPending only covered the (near-instant) scheduling call,
    // not the round-trip — disabled={isPending} was effectively a no-op, the
    // switcher stayed clickable mid-fetch, and a slower earlier response
    // could overwrite a later one. React 19's startTransition accepts an
    // async scope function directly and keeps isPending true for its whole
    // duration, so awaiting here makes the switcher genuinely single-flight
    // (a second currency can't be selected until this one settles) instead
    // of layering on a separate request-id guard.
    startTransition(async () => {
      try {
        const nextSummary = await getPortfolioSummary(next);
        setSummary(nextSummary);
        setLoadError(false);
      } catch {
        setLoadError(true);
        // Revert the switcher to the currency the displayed data actually
        // reflects — otherwise a failed refetch leaves the dropdown showing
        // the new currency while every figure on the page is still the old
        // one.
        setCurrency(summary.base_currency as BaseCurrency);
      }
    });
  };

  // Translate only the display label, not the data key it's built from — a
  // user's own group/broker named the same as the translated fallback
  // ("未分组") must not collapse into the real Ungrouped/Other slice
  // (Grok review round 2, PR #322). asset_class codes are a closed,
  // backend-derived enum (never user free text), so this specific
  // collision can't happen today, but pre-transforming the Record's keys
  // here carried the identical latent risk as the round-2 bug — round-3
  // review leftover, applying the same labelFor fix for consistency.
  const assetClassLabelFor = (code: string) => assetClassLabel(t, code);
  const groupLabelFor = (key: string) => (key === GROUP_UNGROUPED_KEY ? t("groupUngrouped") : key);
  const accountLabelFor = (key: string) => (key === ACCOUNT_OTHER_KEY ? t("accountOther") : key);
  const { priced, noLivePrice } = partitionHoldings(summary.holdings);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-heading text-2xl font-medium">{t("pageTitle")}</h1>
        <CurrencySwitcher value={currency} onChange={handleCurrencyChange} disabled={isPending} />
      </div>

      <PriceAsOfBanner priceAsOfDate={summary.price_as_of_date} />

      {loadError && (
        <p role="alert" className="text-sm text-destructive">
          {t("refreshError")}
        </p>
      )}

      <Card>
        <CardHeader>
          <CardTitle>{t("totalAssetsLabel")}</CardTitle>
        </CardHeader>
        <CardContent className="px-4">
          <span className="font-heading text-3xl tabular-nums">
            {formatMoney(summary.total_base, summary.base_currency)}
          </span>
        </CardContent>
      </Card>

      <PnlSummaryCard summary={summary} />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <BreakdownChart
          title={t("chartByMarket")}
          data={summary.by_market}
          currency={summary.base_currency}
          emptyLabel={t("chartEmpty")}
        />
        <BreakdownChart
          title={t("chartByGroup")}
          data={summary.by_group}
          labelFor={groupLabelFor}
          currency={summary.base_currency}
          emptyLabel={t("chartEmpty")}
        />
        <BreakdownChart
          title={t("chartByCustodian")}
          data={summary.by_account}
          labelFor={accountLabelFor}
          currency={summary.base_currency}
          emptyLabel={t("chartEmpty")}
        />
        <BreakdownChart
          title={t("chartByAssetClass")}
          data={summary.by_asset_class}
          labelFor={assetClassLabelFor}
          currency={summary.base_currency}
          emptyLabel={t("chartEmpty")}
        />
        <BreakdownChart
          title={t("chartBySector")}
          description={t("chartBySectorScope")}
          data={summary.by_sector}
          currency={summary.base_currency}
          emptyLabel={t("chartEmpty")}
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t("holdingsHeading")}</CardTitle>
          <CardDescription>{t("holdingsPnlNote")}</CardDescription>
        </CardHeader>
        <CardContent className="px-4">
          <PortfolioHoldingsTable holdings={priced} baseCurrency={summary.base_currency} />
        </CardContent>
      </Card>

      <NoLivePriceSection holdings={noLivePrice} />
    </div>
  );
}
