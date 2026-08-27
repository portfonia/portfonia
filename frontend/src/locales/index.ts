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

export const DEFAULT_LOCALE: Locale = "en";

// Every catalog-backed locale, in switcher display order — used only where
// "does a catalog exist for this locale" is the question (the structural
// shape-lock test, Object.keys(catalogs) elsewhere). Not used for anything a
// real user can reach; see LOCALES below.
const ALL_LOCALE_META: { value: Locale; label: string }[] = [
  { value: "en", label: "English" },
  { value: "zh-Hans", label: "简体中文" },
  { value: "zh-Hant", label: "繁體中文" },
];

// Locales pending native-speaker review (issue #209 requirement) — drafted
// this session, not yet reviewed. Excluding a locale here does not remove
// its catalog file or drop it from the structural shape-lock test; it only
// blocks every path a real user could reach it through (the switcher below,
// and isLocale() — which also gates restoring a stored value and resolving
// a Server Action's locale form field). Move a locale out of this list once
// it's signed off (see this directory's README's "zh-Hant review status").
const UNREVIEWED_LOCALES: readonly Locale[] = ["zh-Hant"];

// Reviewer finding (blacktomb42, PR #226): ALL_LOCALE_META used to be
// exposed directly as the switcher's options, so an "unreviewed" locale was
// still user-selectable in practice — the README's caveat was documentation,
// not enforcement. LOCALES is the enforcement: every user-facing consumer
// (the switcher in site-header.tsx, and isLocale() below) reads this
// filtered list instead.
export const LOCALES = ALL_LOCALE_META.filter(
  (l) => !UNREVIEWED_LOCALES.includes(l.value),
);

export function isLocale(value: string): value is Locale {
  return LOCALES.some((l) => l.value === value);
}

export type Messages = typeof en;

export const catalogs: Record<Locale, Messages> = {
  en,
  "zh-Hans": zhHans,
  "zh-Hant": zhHant,
};
