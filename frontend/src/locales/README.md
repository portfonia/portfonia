# UI message catalog (issue #209)

One locale-keyed catalog for every in-product string, read via
[next-intl](https://next-intl.dev)'s `useTranslations()` / `useLocale()` /
`t.raw()`. Replaces the two ad hoc TS maps this issue supersedes:
`lib/i18n/home-messages.ts` (home marketing copy, `{en, zh}`) and
`lib/messages.ts` (app chrome, English-only) — both deleted in the same PR.

## Files

- `en.json` — source of truth for shape. Every other locale must carry the
  exact same key paths (enforced by `locales.test.ts`).
- `zh-Hans.json` — Simplified Chinese. Renamed from the old `zh` locale;
  BCP-47 tag now matches `backend/config/i18n_glossary.yml`.
- `zh-Hant.json` — Traditional Chinese, the first locale added beyond
  en/zh-Hans. **LLM-drafted, pending native-speaker review before merge** —
  see "zh-Hant review status" below. Not a mechanical simplified-to-traditional
  character conversion: several terms use Taiwan-standard financial
  vocabulary that differs in wording, not just glyphs (e.g. 那斯達克 vs
  纳指/纳斯达克, 港幣 vs 港元, 積體電路/晶圆代工廠 phrasing).
- `index.ts` — `Locale` type, `LOCALES` (switcher metadata), `catalogs`
  (the `Record<Locale, Messages>` next-intl consumes), `Messages` type
  (derived from `en.json` — the shape source of truth).

## Namespaces

- `common` — brand name.
- `menu` — the one Get Started / account menu used on every route (home and
  app chrome no longer have separate label sets — that split was the root
  cause of the mixed EN/ZH menu bug, issue #207/PR #208).
- `auth` — `/login`, `/signup` pages and their Server Actions.
- `holdings` — `/holdings` page.
- `questionnaire` — `/questionnaire` page.
- `home` — marketing-page-only body content (hero, how-it-works, sample
  report preview, boundary, FAQ, footer). Nothing outside `/` reads this
  namespace.

## No URL-based locale routing (explicit product decision, 2026-08-27)

Concept & Design's frontend engineering constraint 3 calls for `next-intl`
with `/en` / `/zh-Hans` / `/zh-Hant` URL prefixes and full SSR of the
selected locale, specifically to avoid a client-only-i18n SEO/first-paint
flash. Issue #209 explicitly required settling this at implementation time
rather than silently keeping the 2026-08-07 (#94) shortcut.

**Decision: keep the existing `localStorage` + client-state mechanism, no
URL change.** This is a deliberate, informed choice to accept that tradeoff,
not a default. Consequence: locale can only be known client-side, so every
route that renders translated text does so from a Client Component (or a
small client wrapper around the translated fragment of an otherwise-Server
Component page, e.g. `/login`, `/signup`, `/holdings`), and first paint
briefly shows the default locale (`en`) before the stored preference is
restored post-hydration — the same flash the `home` page already accepted
under the #94 shortcut, now extended to every route. If this tradeoff is
revisited, expect a real migration project (route groups under
`app/[locale]/`, moving locale resolution into `proxy.ts`), not a quick flag
flip.

## Placeholders (ICU via next-intl)

`home-messages.ts`/`messages.ts` held a few values as functions (e.g.
`uploadingProgress(seconds)`, `previewValidCount(n)`). JSON can't hold
functions, so these became either:

- ICU plural syntax read with plain `t()` (e.g. `holdings.previewValidCount`,
  `holdings.issuesCount`, `holdings.confirmBody`) — English needs `one`/
  `other` plural branches; the Chinese locales only need `other` (CLDR zh has
  no distinct plural forms), so their catalog values interpolate `{n}`
  directly without an ICU `plural` wrapper — both are valid to feed the same
  `t()` call.
- Plain `{placeholder}` interpolation (`menu.sessionExpired`,
  `questionnaire.stepOf`).
- A small object of pre-branched strings for cases where the original
  function's threshold logic (not just interpolation) drove which sentence
  showed — `holdings.uploadingProgress.{reading,parsing,stillWorking,slow}`.
  The threshold logic itself stays in `holdings-manager.tsx`, since it's UI
  behavior, not translatable content.

Sample report preview data (`home.preview.holdingsRows`, `anomalyRows`,
`technicalRows`, `calendarRows`, `distributionLines`, and `home.how.cards` /
`home.faq.items` / `home.boundary.items`) are plain arrays/objects read with
`t.raw()`, not `t()` — they're structured mock data for the marketing page,
not ICU message strings.

## Overlapping terms with the report glossary

`backend/config/i18n_glossary.yml`'s `report_glossary` is the authority for
terms that appear in both the UI and AI-generated reports: `Custodian` →
`持仓机构` (zh-Hans), and the confidence-tier tags `[Established]` /
`[Probable]` / `[Speculative]`. This catalog's `home.preview.holdingsColumns`
and `home.how.tiers` values must match it. There is no shared source file
across the Python/YAML and TypeScript/JSON runtime boundary — drift is
caught by `glossary-consistency.test.ts`, which parses the YAML at test time
and asserts equality against this catalog's zh-Hans values for those keys.
(Fixed as part of #209: `home.preview.holdingsColumns` previously said
`托管机构`, which had drifted from the glossary's `持仓机构` — a live example
of the drift this test now prevents.)

zh-Hant has no such comparison today: `i18n_glossary.yml`'s `zh-Hant` column
is still reserved/empty (report output stays zh-Hans-only per this issue's
scope — see Concept & Design's Ring 1 note). If report output ever grows a
real zh-Hant column, revisit whether these UI terms should be the seed.

## zh-Hant review status

Every string in `zh-Hant.json` was drafted by an LLM for this PR, not
translated or reviewed by a native Traditional Chinese speaker. Per the
issue's explicit requirement, this must not be treated as ship-ready
sign-off — it needs human review (ideally Taiwan or HK usage) before this
locale is exposed to real users. The PR description lists this file as the
review item.

## Adding a fourth locale

1. Add `<locale>.json` here with the same key paths as `en.json` (copy `en`
   or `zh-Hant` as a starting shape, then translate — `locales.test.ts` will
   fail loudly on any missing/extra key).
2. Add it to `LOCALES` and the `catalogs` map in `index.ts`.

No page or component changes — every route already reads through
`useTranslations()` against whatever locale is selected.
