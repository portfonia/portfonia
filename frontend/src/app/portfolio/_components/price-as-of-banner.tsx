"use client";

import { useTranslations } from "next-intl";

// Wording spirit borrowed from report_sections.py's _data_window_line()
// (issue #320's design notes): never imply real-time data. priceAsOfDate is
// null whenever THIS run matched no captured close that actually priced a
// row — an empty book, a capture-unsupported-only book, cash/wmf-only, or
// an auto holding still waiting on its first snapshot. Grok review round 2
// (PR #322): round-1's copy for this case named "cash and wealth-management
// values" specifically, which is wrong for the other reasons above — kept
// generic instead of guessing which one applies.
export function PriceAsOfBanner({ priceAsOfDate }: { priceAsOfDate: string | null }) {
  const t = useTranslations("portfolio");
  return (
    <div className="rounded-md border border-input bg-muted/50 px-3 py-2 text-sm text-foreground/80">
      {priceAsOfDate ? t("asOfBanner", { date: priceAsOfDate }) : t("asOfBannerUnavailable")}
    </div>
  );
}
