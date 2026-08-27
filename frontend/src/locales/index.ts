import en from "./en.json";
import zhHans from "./zh-Hans.json";
import zhHant from "./zh-Hant.json";

// The one UI message catalog (issue #209) — every current and future route
// reads copy from here via next-intl's useTranslations(), instead of the two
// ad hoc TS maps (home-messages.ts / messages.ts) this replaces. en.json's
// shape is the source of truth; locales.test.ts regression-locks that
// zh-Hans and zh-Hant never drift from it structurally.
//
// This is a UI catalog only — distinct from backend/config/i18n_glossary.yml
// (the report-translation glossary). Overlapping terms (Custodian,
// [Established]/[Probable]/[Speculative]) are kept in sync by convention and
// checked by glossary-consistency.test.ts, not by a shared source file: the
// two catalogs serve different runtimes (React vs the Python translation
// pass) with different shapes (keyed chrome with ICU interpolation vs
// EN-term-to-locale rendering pairs).
export type Locale = "en" | "zh-Hans" | "zh-Hant";

export const LOCALES: { value: Locale; label: string }[] = [
  { value: "en", label: "English" },
  { value: "zh-Hans", label: "简体中文" },
  { value: "zh-Hant", label: "繁體中文" },
];

export const DEFAULT_LOCALE: Locale = "en";

export function isLocale(value: string): value is Locale {
  return LOCALES.some((l) => l.value === value);
}

export type Messages = typeof en;

export const catalogs: Record<Locale, Messages> = {
  en,
  "zh-Hans": zhHans,
  "zh-Hant": zhHant,
};
