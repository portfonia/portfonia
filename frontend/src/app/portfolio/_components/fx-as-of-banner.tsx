"use client";

import { TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";

// Sibling of PriceAsOfBanner (issue #354): one combined line, one date per
// currency actually needed for this render's conversions (never USD — see
// PortfolioSummary.fx_rates_as_of). Omitted entirely when that map is empty.
// Issue #532: stale_fx_pairs only annotates currencies already in the map.
// The backend classifies the 48-hour threshold; a stale rate still values
// the book, so the warning state is disclosure, not an error.
export function FxAsOfBanner({
  fxRatesAsOf,
  staleFxPairs,
}: {
  fxRatesAsOf: Record<string, string>;
  staleFxPairs: string[];
}) {
  const t = useTranslations("portfolio");
  const entries = Object.entries(fxRatesAsOf).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) {
    return null;
  }
  const stale = new Set(staleFxPairs);
  const warning = entries.some(([currency]) => stale.has(currency));
  const list = entries
    .map(([currency, date]) =>
      stale.has(currency)
        ? t("fxAsOfEntryStale", { currency, date })
        : t("fxAsOfEntry", { currency, date }),
    )
    .join(" · ");
  return (
    <div
      className={
        warning
          ? "flex items-start gap-2 rounded-md border border-amber-300/60 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200"
          : "rounded-md border border-input bg-muted/50 px-3 py-2 text-sm text-foreground/80"
      }
    >
      {warning ? <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /> : null}
      {warning ? t("fxAsOfBannerWarning", { list }) : t("fxAsOfBanner", { list })}
    </div>
  );
}
