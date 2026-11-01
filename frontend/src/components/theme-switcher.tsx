"use client";

import { ChevronDown, Monitor, Moon, Sun } from "lucide-react";
import { useTranslations } from "next-intl";
import { MenuDropdown, MenuItemCheckbox } from "@/components/ui/menu";
import type { ThemePreference } from "@/lib/theme";

type ThemeProps = { preference: ThemePreference; onSelect: (next: ThemePreference) => void };
const OPTIONS = [
  { value: "dark", key: "themeDark", Icon: Moon },
  { value: "light", key: "themeLight", Icon: Sun },
  { value: "system", key: "themeSystem", Icon: Monitor },
] as const;

export function ThemeMenuItems({ preference, onSelect }: ThemeProps) {
  const t = useTranslations("menu");
  return OPTIONS.map(({ value, key }) => (
    <MenuItemCheckbox key={value} checked={preference === value} onCheckedChange={() => onSelect(value)}>
      {t(key)}
    </MenuItemCheckbox>
  ));
}

export function ThemeSwitcher({ preference, onSelect }: ThemeProps) {
  const t = useTranslations("menu");
  const current = OPTIONS.find(option => option.value === preference) ?? OPTIONS[2];
  const Icon = current.Icon;
  return (
    <MenuDropdown triggerClassName="hidden sm:inline-flex" trigger={
      <>
        <Icon aria-hidden="true" className="size-4" />
        <span className="sr-only">{t("theme")}: {t(current.key)}</span>
        <ChevronDown aria-hidden="true" className="size-4 opacity-80" />
      </>
    }>
      <ThemeMenuItems preference={preference} onSelect={onSelect} />
    </MenuDropdown>
  );
}
