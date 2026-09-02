"use client";

import { useTranslations } from "next-intl";

// Wording spirit borrowed from report_sections.py's _data_window_line()
// (issue #320's design notes): never imply real-time data. priceAsOfDate is
// null only when nothing has ever been captured for this user.
export function PriceAsOfBanner({ priceAsOfDate }: { priceAsOfDate: string | null }) {
  const t = useTranslations("portfolio");
  return (
    <div className="rounded-md border border-input bg-muted/50 px-3 py-2 text-sm text-foreground/80">
      {priceAsOfDate ? t("asOfBanner", { date: priceAsOfDate }) : t("asOfBannerUnavailable")}
    </div>
  );
}
