"use client";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { REPLAY_RANGES, type ReplayRange } from "@/lib/api";

export function ReplayRangeTabs({ value, onChange, disabled }: {
  value: ReplayRange; onChange: (range: ReplayRange) => void; disabled?: boolean;
}) {
  const t = useTranslations("jade.replay");
  return <div role="radiogroup" aria-label={t("rangeLabel")} className="flex flex-wrap items-center gap-1">
    {REPLAY_RANGES.map(range => <Button key={range} type="button" size="sm"
      variant={range === value ? "default" : "outline"} aria-pressed={range === value}
      disabled={disabled} onClick={() => onChange(range)}>{t(`ranges.${range}`)}</Button>)}
  </div>;
}
