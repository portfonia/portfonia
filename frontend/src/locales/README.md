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
- `emailVerification` — `/verify-email` page (issue #260).
- `unsubscribe` — `/unsubscribe` page (issue #257).

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

Every string in `zh-Hant.json` was originally drafted by an LLM, not
translated or reviewed by a native Traditional Chinese speaker. The
original PR (#226) held it behind an `UNREVIEWED_LOCALES` gate in
`index.ts` for exactly that reason — see the "Enforced, not just
documented" history below, kept for context.

**Gate lifted (issue #350 item 4, deliberate product-owner decision):**
`UNREVIEWED_LOCALES` is now empty — `zh-Hant` is a fully supported,
user-selectable locale, matching `isLocale()`'s and the switcher's
behavior for `en`/`zh-Hans`. This knowingly reverses the "pending
native-speaker review" hold below without a review having actually
happened; it was raised and confirmed explicitly with the product owner
during issue #350's design conversation, not an oversight. Do not
reintroduce the gate or re-litigate this decision without the same kind
of explicit sign-off.

**Original hold + its enforcement mechanism (PR #226, for history):** an
earlier version of that PR had `zh-Hant` in `index.ts`'s exported
`LOCALES` list, which is what the switcher (`site-header.tsx`) and
`isLocale()` both read — so the unreviewed locale was still
user-selectable in practice despite the section above. `index.ts` was
fixed to keep `zh-Hant.json`'s catalog and its coverage in
`locales.test.ts`'s structural shape-lock (so it can't silently drift),
while excluding it from `LOCALES` via the (now-empty)
`UNREVIEWED_LOCALES` list — which is also why `isLocale()` blocked
resolving a stored `zh-Hant` value from `localStorage` or a Server
Action's locale form field, not just hiding the switcher option. The
mechanism itself (an `UNREVIEWED_LOCALES` list gating `LOCALES`) is
unchanged and available again for a future locale that needs the same
treatment — see "Adding a fourth locale" below.

## Adding a fourth locale

1. Add `<locale>.json` here with the same key paths as `en.json` (copy `en`
   or `zh-Hant` as a starting shape, then translate — `locales.test.ts` will
   fail loudly on any missing/extra key).
2. Add it to `ALL_LOCALE_META` and the `catalogs` map in `index.ts`. If it
   needs the same not-yet-reviewed treatment as `zh-Hant` above, also add it
   to `UNREVIEWED_LOCALES` — otherwise it's immediately user-selectable.

No page or component changes — every route already reads through
`useTranslations()` against whatever locale is selected.
