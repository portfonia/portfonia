"use client";

import { ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";

import { MenuDropdown, MenuItemButton } from "@/components/ui/menu";
import { BENCHMARK_CODES, type BenchmarkCode } from "@/lib/api";

// Independent single-select for the monthly-performance card's one
// benchmark (issue #433 requirement 5) — deliberately not a variant of
// MultiSelectMenu: this control can never reach "none" or "several", so it
// follows the same MenuDropdown/MenuItemButton single-choice pattern
// CurrencySwitcher already uses rather than reusing the checkbox-list menu.
export function BenchmarkSingleSelectMenu({
  label,
  value,
  onChange,
  disabled,
}: {
  label: string;
  value: BenchmarkCode;
  onChange: (next: BenchmarkCode) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("portfolio");
  const names = t.raw("performance.benchmarkNames") as Record<BenchmarkCode, string>;

  return (
    <MenuDropdown
      disabled={disabled}
      trigger={
        <>
          <span>{label}</span>
          <span className="font-normal text-foreground/70">{names[value]}</span>
          <ChevronDown aria-hidden="true" className="size-4 opacity-80" />
        </>
      }
    >
      {BENCHMARK_CODES.map((code) => (
        <MenuItemButton key={code} onClick={() => onChange(code)}>
          {names[code]}
        </MenuItemButton>
      ))}
    </MenuDropdown>
  );
}
