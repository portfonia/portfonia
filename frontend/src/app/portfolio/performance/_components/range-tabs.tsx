"use client";

import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { PERFORMANCE_RANGES, type PerformanceRange } from "@/lib/api";

// Range selector (1M/6M/YTD/1Y/5Y/ALL — issue #360 requirement 3; no 1D/5D:
// this app captures no intraday data, D4). A radiogroup of aria-pressed
// buttons matches the repo's existing exclusive-choice controls (holdings
// upload mode, questionnaire options) rather than inventing a tablist.
export function RangeTabs({
  value,
  onChange,
  disabled,
}: {
  value: PerformanceRange;
  onChange: (range: PerformanceRange) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("portfolio");
  const labelFor: Record<PerformanceRange, string> = {
    "1M": t("performance.rangeOneM"),
    "6M": t("performance.rangeSixM"),
    YTD: t("performance.rangeYtd"),
    "1Y": t("performance.rangeOneY"),
    "5Y": t("performance.rangeFiveY"),
    ALL: t("performance.rangeAll"),
  };

  return (
    <div
      role="radiogroup"
      aria-label={t("performance.rangeLabel")}
      className="flex flex-wrap items-center gap-1"
    >
      {PERFORMANCE_RANGES.map((range) => (
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
