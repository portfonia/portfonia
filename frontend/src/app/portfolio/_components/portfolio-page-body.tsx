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
import { formatMoney, partitionHoldings } from "./portfolio-helpers";
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

  const handleCurrencyChange = (next: BaseCurrency) => {
    setCurrency(next);
    startTransition(() => {
      void getPortfolioSummary(next)
        .then((nextSummary) => {
          setSummary(nextSummary);
          setLoadError(false);
        })
        .catch(() => {
          setLoadError(true);
        });
    });
  };

  if (loadError && !summary) {
    return <p className="text-sm text-destructive">{t("loadError")}</p>;
  }
  if (!summary) {
    return null;
  }

  const byAssetClassLabeled = Object.fromEntries(
    Object.entries(summary.by_asset_class).map(([code, value]) => [
      assetClassLabel(t, code),
      value,
    ]),
  );
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
          currency={summary.base_currency}
          emptyLabel={t("chartEmpty")}
        />
        <BreakdownChart
          title={t("chartByCustodian")}
          data={summary.by_account}
          currency={summary.base_currency}
          emptyLabel={t("chartEmpty")}
        />
        <BreakdownChart
          title={t("chartByAssetClass")}
          data={byAssetClassLabeled}
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
