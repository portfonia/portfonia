"use client";

import { useTranslations } from "next-intl";

// Sibling of PriceAsOfBanner (issue #354, §8.5 of the design doc): one
// combined line, one date per currency actually needed for this render's
// conversions (never USD — see PortfolioSummary.fx_rates_as_of). Mixed
// dates across currencies are the accepted, permanent normal state of the
// per-pair independent FX resolution this issue fixed — this banner exists
// for transparency about that, not to flag an error. Omitted entirely when
// empty (single-currency book already matching base_currency, no
// conversion happened) — mirrors PriceAsOfBanner's hide-when-inapplicable
// behavior rather than inventing a new "N/A" state.
export function FxAsOfBanner({ fxRatesAsOf }: { fxRatesAsOf: Record<string, string> }) {
  const t = useTranslations("portfolio");
  const entries = Object.entries(fxRatesAsOf).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) {
    return null;
  }
  const list = entries.map(([currency, date]) => `${currency} as of ${date}`).join(" · ");
  return (
    <div className="rounded-md border border-input bg-muted/50 px-3 py-2 text-sm text-foreground/80">
      {t("fxAsOfBanner", { list })}
    </div>
  );
}
