"use client";

import { ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";

import {
  MenuDropdown,
  MenuItemCheckbox,
  MenuSeparator,
} from "@/components/ui/menu";

export interface MultiSelectOption {
  value: string;
  label: string;
}

// Multi-select dropdown built on the shared MenuDropdown/Menu.CheckboxItem
// pattern (issue #360 Phase 2 — benchmark and dataset-dimension filters).
// Items stay open while toggling (Base UI CheckboxItem does not close on
// click by default) so several options can be adjusted in one pass.
//
// What "All" means is mode-specific, because the API's two consumers have
// opposite omit semantics (review 5128075545 finding 1):
// - `allMode="none"` (dataset dimensions): empty selection = All = omit the
//   filter param, which the router reads as "no filter". The All row is
//   checked while nothing is selected.
// - `allMode="all-options"` (benchmarks): omitting the param means NO
//   benchmarks (the router expands [] to zero series — a valid
//   portfolio-only view, issue #382). All must mean every option
//   selected. Clicking All selects all options; unchecking chips can
//   reach [].
export function MultiSelectMenu({
  label,
  options,
  selected,
  onChange,
  disabled,
  allMode = "none",
}: {
  label: string;
  options: MultiSelectOption[];
  selected: readonly string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
  allMode?: "none" | "all-options";
}) {
  const t = useTranslations("portfolio");
  const allSelected =
    allMode === "all-options"
      ? options.length > 0 && selected.length === options.length
      : selected.length === 0;

  const toggle = (value: string) => {
    onChange(
      selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value],
    );
  };

  // Clicking an already-active All row is a no-op (same as the pre-review
  // dimension behavior: an "All" state is applied, not toggled off through
  // this row — clearing every code is done by unchecking the options).
  const applyAll = () => {
    if (allSelected) return;
    onChange(allMode === "all-options" ? options.map((option) => option.value) : []);
  };

  const summary = allSelected
    ? t("performance.selectionAll")
    : t("performance.selectionCount", { count: selected.length });

  return (
    <MenuDropdown
      disabled={disabled}
      trigger={
        <>
          <span>{label}</span>
          <span className="font-normal text-foreground/70">{summary}</span>
          <ChevronDown aria-hidden="true" className="size-4 opacity-80" />
        </>
      }
    >
      <MenuItemCheckbox checked={allSelected} onCheckedChange={applyAll}>
        {t("performance.selectionAll")}
      </MenuItemCheckbox>
      <MenuSeparator />
      {options.map((option) => (
        <MenuItemCheckbox
          key={option.value}
          checked={selected.includes(option.value)}
          onCheckedChange={() => toggle(option.value)}
        >
          {option.label}
        </MenuItemCheckbox>
      ))}
    </MenuDropdown>
  );
}
