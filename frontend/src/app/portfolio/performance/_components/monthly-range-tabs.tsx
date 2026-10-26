"use client";

import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { MONTHLY_RANGES, type MonthlyRange } from "@/lib/api";

export function MonthlyRangeTabs({
  value,
  onChange,
  disabled,
}: {
  value: MonthlyRange;
  onChange: (range: MonthlyRange) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("portfolio");
  const labelFor: Record<MonthlyRange, string> = {
    "6M": t("performance.rangeSixM"),
    "1Y": t("performance.rangeOneY"),
    "3Y": t("performance.rangeThreeY"),
  };

  return (
    <div
      role="radiogroup"
      aria-label={t("performance.rangeLabel")}
      className="flex flex-wrap items-center gap-1"
    >
      {MONTHLY_RANGES.map((range) => (
        <Button
          key={range}
          type="button"
          variant={range === value ? "default" : "outline"}
          size="sm"
          aria-pressed={range === value}
          disabled={disabled}
          onClick={() => onChange(range)}
        >
          {labelFor[range]}
        </Button>
      ))}
    </div>
  );
}
