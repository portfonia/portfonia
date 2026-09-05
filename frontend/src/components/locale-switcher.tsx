"use client";

// Issue #350 item 4: rebuilt to match GetStartedMenu's styling (the shared
// Base UI Menu primitives in components/ui/menu.tsx) instead of a plain
// native <select> — visually inconsistent with the menu it sat next to.
// Flag icons are image-based (flag-icons npm package, MIT, SVG,
// ISO 3166-1-alpha-2 codes) rather than OS/system emoji glyphs: system
// emoji flag rendering is unreliable across platforms (confirmed a real
// problem by the product owner during this issue's design conversation),
// so a plain "🇺🇸" text glyph was never on the table as an alternative here.
import "flag-icons/css/flag-icons.css";
import { ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";

import { MenuDropdown, MenuItemButton } from "@/components/ui/menu";
import { useLocale } from "@/app/_components/locale-provider";
import { LOCALES, type Locale } from "@/locales";

// Locale -> ISO 3166-1-alpha-2 country code for flag-icons' `.fi-<code>`
// class. English -> `us`, not `gb`: English represents the product's base
// locale, not the language's country of origin (explicit product-owner
// choice, confirmed during this issue's design conversation — do not
// second-guess during a future edit). Traditional Chinese -> `tw` (Taiwan),
// also an explicit product-owner instruction, not a default assumption.
const FLAG_CODE: Record<Locale, string> = {
  en: "us",
  "zh-Hans": "cn",
  "zh-Hant": "tw",
};

function Flag({ locale }: { locale: Locale }) {
  return (
    <span
      aria-hidden="true"
      className={`fi fi-${FLAG_CODE[locale]} shrink-0 rounded-[2px]`}
    />
  );
}

export function LocaleSwitcher() {
  const tMenu = useTranslations("menu");
  const { locale, setLocale } = useLocale();
  const current = LOCALES.find((l) => l.value === locale) ?? LOCALES[0];

  return (
    <MenuDropdown
      trigger={
        <>
          <Flag locale={current.value} />
          {/* Visually hidden, but still part of the trigger button's
              accessible name — the visible content is flag + chevron only,
              matching the compact icon-button treatment this control needs
              next to Get Started's full-width text trigger. */}
          <span className="sr-only">
            {tMenu("language")}: {current.label}
          </span>
          <ChevronDown aria-hidden="true" className="size-4 opacity-80" />
        </>
      }
    >
      {LOCALES.map((l) => (
        <MenuItemButton key={l.value} onClick={() => setLocale(l.value)}>
          <Flag locale={l.value} />
          {l.label}
        </MenuItemButton>
      ))}
    </MenuDropdown>
  );
}
