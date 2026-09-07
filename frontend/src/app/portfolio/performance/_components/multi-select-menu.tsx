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
// Selection semantics follow the API's: an empty selection sends no filter
// param = "all". The first row is an explicit "All" checkbox (checked while
// nothing is selected) so that meaning is discoverable instead of implied by
// an empty list.
export function MultiSelectMenu({
  label,
  options,
  selected,
  onChange,
  disabled,
}: {
  label: string;
  options: MultiSelectOption[];
  selected: readonly string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("portfolio");
  const allSelected = selected.length === 0;

  const toggle = (value: string) => {
    onChange(
      selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value],
    );
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
      <MenuItemCheckbox
        checked={allSelected}
        onCheckedChange={(checked) => {
          if (checked) onChange([]);
        }}
      >
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
