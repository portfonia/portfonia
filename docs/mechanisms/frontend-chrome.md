# Frontend chrome (header/nav)

### Frontend chrome (header/nav) convention — implemented (issue #146/#148)

**Every route shares ONE header/nav component (`frontend/src/components/site-header.tsx`),
rendered once from the root `app/layout.tsx`.** Do not add a second
per-route header implementation — that duplication (`HomeNav` vs a separate
`SiteHeader`) is exactly the bug this convention fixed; see Obsidian
`Hermes/Portfonia/Portfonia Concept & Design.md` §10 addendum (2026-08-14)
for the full before/after and decision rationale.

- **Current shape**: `SiteHeader` renders as a `<header>` landmark
  (floating rounded pill, `sticky top-4`, backdrop-blur, dark). `AppShell`
  (`frontend/src/app/_components/app-shell.tsx`, renamed from the old
  home-only `HomeShell`) and `LocaleProvider` both wrap the whole app from
  root layout, not just the home page. Universal on every route:
  brand/home link and the Get Started dropdown menu
  (`components/get-started-menu.tsx` — auth-gated entry registry: guest
  sees only Log in; authed sees the full `AUTHED_ENTRIES` list (Profile,
  Holdings, Portfolio, Questionnaire, as of issue #320/PR #322 — the
  standalone Edit holdings entry issue #320/PR #322 originally added
  alongside Portfolio was removed by issue #319/PR #321's nav-entry
  dedup, which landed and merged first; see that entry's own PR for the
  count each new route added) plus
  email + Log out, in that order — issue #214 follow-up originally added a
  "Home" entry as an explicit way back to `/` from any inner page,
  replaced by Profile in issue #220 (see below)). Home-only
  (`pathname === "/"`): the locale
  switcher, plus the brand link's target changes to `#top` (in-page jump)
  instead of `/`. The four marketing anchor links were REMOVED from the
  bar (issue #207) — the marketing sections remain on the home page
  itself, reachable by scrolling, not via bar shortcuts. The old
  `AuthStatus` component is deleted; the Holdings standalone button is
  gone (Holdings lives inside the menu).
- **Session display trusts only a verified `getUser()`** (`hooks/use-
  session.ts`) — never the locally-cached `INITIAL_SESSION`/`SIGNED_IN`
  event payload. Re-verifies on: mount, focus/visibilitychange, an
  `onAuthStateChange` event fired by the browser-side SDK itself, AND
  (issue #214) every `pathname` change — `login()`/`logout()` are Server
  Actions that `redirect()`, and `SiteHeader` lives in the shared root
  layout so it never remounts across that navigation; the browser-side SDK
  never sees the server-side sign-in/out either, so without the pathname
  signal the menu would only ever catch up via the focus/visibility
  fallback, on no deterministic schedule. **Every pathname change
  re-verifies unconditionally — there is no throttling.** A grace window
  that collapsed rapid-navigation bursts into one re-verify shipped
  briefly (PR #215 review) and was reverted the same day (2026-08-26,
  real user report): the same window that collapses several link clicks
  into one call also swallowed the one pathname change that actually
  mattered — the redirect right after a real login or logout — since a
  login/logout round-trip routinely completes faster than the window. The
  menu was left showing the pre-action state (guest right after logging
  in, still-authed right after logging out) until an unrelated
  focus/visibility event happened to fire. The at-most-a-few-extra-calls
  cost of re-verifying on every hop is cheaper than that. Two
  purpose-built signals cover the two actions that are otherwise
  invisible to the next page's `useSession` instance (the component that
  triggered them is already gone by the time it mounts): `markPendingLogin()`
  (login form's `onSubmit`) tags the next `checking` window with
  `pendingReason: "login"` so the menu shows a "Logging in..." placeholder
  instead of nothing during the post-redirect verification. Signup uses
  the same `markPendingLogin()` signal (post-signup redirects to
  `/questionnaire?onboarding=1` as of issue #221 — see that section below,
  not `/holdings` anymore). `clearPendingLogin()` disarms it if login/signup returns
  an error instead of redirecting, so a later ordinary navigation does
  not show a stale "Logging in..." placeholder. `markOptimisticLogout()`
  (Log out button's `onClick`) flips the state to `guest` immediately so
  the click is not gated on the round-trip. The Server Action can still
  fail (`signOut()` / network); the click handler catches a non-redirect
  rejection, calls `revalidateSession()` to drop the optimistic guest
  state, and shows a visible error. `redirect()`'s `NEXT_REDIRECT` throw
  is success, not failure.
  `getUser()` itself is bound by an 8s timeout + one retry (timeout only,
  not on an immediate network error) — `auth.portfonia.com` (the Caddy
  reverse-proxy routing around direct Supabase connectivity issues) has
  been observed spiking
  well past its normal sub-second response under network jitter.
- **`lang` attribute is route-scoped, not just component-scoped**:
  `AppShell` only follows the selected locale on `/`; every other route
  (still English-only via `lib/messages.ts`, no `zh` map yet) stays
  `lang="en"` regardless of what's stored in `localStorage` — a stored
  `zh` from the home page must never mislabel `/holdings`' English content
  for screen readers / in-browser translate. Covered by
  `app-shell.test.tsx`.
- **The locale switcher itself is home-only for the same reason**:
  `/holdings`' `lib/messages.ts` has no `zh` map yet; showing the switcher
  everywhere before that's fixed would let a user "switch language" on a
  page whose text never changes. Relax that gate once `messages.ts` gains
  a `zh` map — separate, unscheduled work.
- **Tests**: `frontend` had no test framework before this — vitest +
  React Testing Library were added specifically for this change
  (`npm run test`). `site-header.test.tsx` / `get-started-menu.test.tsx`
  / `app-shell.test.tsx` lock the route-conditional rendering and the
  auth-gated menu above; extend them, don't remove the
  route-parametrized assertions, if this component changes again.
- When adding a new route: it inherits the header for free by living
  under the root layout — do not wrap it in its own header/layout unless
  it has a genuine reason to opt out of the shared chrome (and if so,
  treat that as worth a design-doc note, not a silent second
  implementation).

### Profile page + menu icons (issue #220, 2026-08-27)

- **`AUTHED_ENTRIES`'s first entry is `profile` → `/profile`, not `home` →
  `/`.** The #214-follow-up placeholder "Home" entry is gone; the way back
  to `/` is the brand-link click only (product confirmation, Obsidian `Ring
  1-Profile Page.md` §三: "Home was always a placeholder"). `menu.home` was
  renamed to `menu.profile` in all three locale catalogs — not added
  alongside it — since nothing else read the old key.
- **Every `GetStartedMenu` entry now carries a `lucide-react` icon**
  (`User`/`Briefcase`/`ClipboardList` for the three authed entries as of
  this issue — `Pencil` and `ChartPie` were added for Edit holdings/
  Portfolio by later PRs, see `AUTHED_ENTRIES` in
  `components/get-started-menu.tsx` for the current, authoritative list;
  `LogIn`/`LogOut` for guest login and manual logout), `aria-hidden="true"`
  since the adjacent label already carries the accessible name.
  `components/ui/menu.tsx`'s `MenuItemLink`/`MenuItemButton` switched from
  `className="block ..."` to `flex items-center gap-2 ...` to lay the icon
  and label out on one line — a shared-component change, not per-entry
  markup, so a future entry gets the same layout for free.
- **`/profile` inherits the shared header for free** (root-layout
  convention above) and is protected by the existing `proxy.ts` gate — it
  is not in `PUBLIC_PATH_PREFIXES`, so an unauthenticated request redirects
  to `/login` with no route-specific code.
- **New `profile` message namespace**, translated into all three locales
  (`en`/`zh-Hans`/`zh-Hant`) from the start — issue #209's global catalog
  already covers every route, so a new route landing English-only would
  have reopened the exact per-route gap #209 closed, not stayed consistent
  with it.
- **`GET /me` full #221 shape** — see `docs/mechanisms/identity-and-auth.md`'s
  "GET /me" entry for the endpoint; this page renders `email` and
  `delivery_email` (with an explicit visible fallback-to-account-email note
  when unset — a product decision made when implementing this issue, not
  specified in the original issue text). At #220 time `missing`/
  `has_questionnaire`/`has_holdings`/`tos_accepted_at` were unused — #221
  (below) is what reads `missing` for the gap card; `has_questionnaire`/
  `has_holdings`/`tos_accepted_at` still have no frontend reader.
- **Profile redesign (issue #269, 2026-08-30):** section order is now gap
  card → Email Verification → Account → Investment style → Delivery email →
  placeholders → Change password → Delete account. Email Verification is
  the second section (right after the gap card slot, whether or not that
  slot renders) and its render condition widened to "actionable
  pending/undeliverable records exist" OR "no verified receiving address at
  all" (both `email_verified_at` and `delivery_email_verified_at` null from
  `GET /me`). Three visual languages, deliberately distinct: neutral
  `Card`, `variant="urgent"` (soft pink fill — "complete this soon",
  gap card + Email Verification; theme-aware `--urgent` token in
  `globals.css`), and `variant="danger"` (thin red ring only, no fill —
  Delete account's GitHub-style danger zone). The delivery-email section
  renders an unverified shown address gray italic with a note and an inline
  Resend button (bound to the matching pending/undeliverable record; the
  overlap with the top section's list is intentional per the issue). Resend
  logic lives in the shared `useVerificationResend` hook — the success path
  clears the in-flight id in a `finally`, so a completed resend re-enables
  every resend button (PR #270 review finding). The no-recipient copy now
  states the send-stop — "Reports will not be sent until an address is
  verified" — since the §3.6 send-time gate is live (issue #276); issue
  #290 lifted the #280-era constraint that kept it in the weaker "so
  reports can reach you" register (issue #269's own "mirrors
  `recipient_email()`" phrasing was inaccurate — corrected in the issue
  thread).
- **Change-password Server Action** (`app/profile/actions.ts`) follows the
  same `signInWithPassword`-then-`updateUser` pattern as
  `Ring 1-Profile Page.md` §三 decision 2 — verifies against the caller's
  own session email (`supabase.auth.getUser()`), never a client-submitted
  `email` form field, so a forged field can't steer whose password gets
  checked.
- **Every non-implemented Profile section (report schedule, delivery-email
  change, invite generation, delete account) is rendered with disabled
  controls**, never a submittable form — issue #220's requirement that
  these stay visible placeholders, not silently absent or falsely
  interactive. Portfolio overview shipped in issue #320/PR #322 — it is a
  real link into `/portfolio`, no longer in this placeholder set (see
  `docs/mechanisms/holdings-pipeline.md`'s C2 section).

### Post-signup onboarding: ToS gate, questionnaire → holdings → welcome, Profile gap card (issue #221, 2026-08-27)

Canonical design: Obsidian `Hermes/Portfonia/Docs/Ring 1-Onboarding.md`.

- **`signup/actions.ts` redirects to `/questionnaire?onboarding=1`**, not
  `/holdings` — the single trigger point for `mode="onboarding"` anywhere
  in the app. `SignupForm` gained a ToS checkbox with a client-side gate
  (mirrors the existing password-mismatch `preventDefault` pattern); the
  backend's `SignupRequest.tos_accepted: Literal[True]` is the independent
  second layer, not a duplicate of the client check. **Update (issue #107,
  PR #271, 2026-08-31)**: the checkbox label now links to `/terms` and
  `/privacy` (both `target="_blank"`, so an in-progress signup form isn't
  lost) — issue #221 shipped the checkbox with no links and no `/tos` body
  page; #107 filled that gap with two separate public pages instead of a
  single `/tos` route. `tosRequired` now names both documents.
- **One implementation per screen, `mode` prop, no `/onboarding/*` tree.**
  `QuestionnaireForm`/`QuestionnairePageBody` take `mode: "onboarding" |
  "edit"` (default `"edit"`); `HoldingsManager` takes `mode: "onboarding" |
  "normal"` (default `"normal"`). Each page reads its own `searchParams.
  onboarding === "1"` (async `searchParams: Promise<...>`, same pattern as
  `signup/page.tsx`'s `invite` param) and passes the resolved mode down —
  there is no shared "onboarding context," each route resolves it locally.
- **Save always navigates away now, in both modes** — questionnaire
  onboarding Save goes to `/holdings?onboarding=1` and holdings onboarding
  Save goes to `/welcome`; edit-mode questionnaire Save goes to `/profile`.
  **Update (issue #280, 2026-08-31)**: the §2.2 table's `onboarding` row was
  wrong — questionnaire onboarding Save used to jump straight to `/welcome`,
  skipping the holdings step entirely, so only a user who *skipped* the
  questionnaire ever saw the holdings page. Save now joins Skip at
  `/holdings?onboarding=1` (design correction recorded in Ring
  1-Onboarding.md §9.1). This supersedes issue #214's
  same-path-Link-no-remount fix (which reset the questionnaire wizard's
  `step` back to 0 instead of navigating): once every successful save
  leaves `/questionnaire`, that fix is unreachable and was removed.
  Skip (a plain `Link`, never a submit — writes no row) follows the same
  table: onboarding → `/holdings?onboarding=1`, edit → `/profile`. Holdings
  onboarding mode additionally gains a "Skip for now" link to `/welcome`
  (§9.1's "持仓页保存/跳过" — a plain `Link`, no rows written), hides the
  Current holdings card (which is also where Export lives, so hiding the
  card hides Export too) and the Download-template button.
- **`/welcome` is a new route**, not public (absent from `proxy.ts`'s
  `PUBLIC_PATH_PREFIXES`, same as `/profile`/`/holdings` — no route-specific
  auth code needed). Server Component `page.tsx` calls `getMeServer()`;
  the Client Component `WelcomeBody` does a `sessionStorage.
  portfonia.welcomed` dedupe check in a `useEffect` (same one-time
  client-only-reveal pattern as `locale-provider.tsx`'s restore effect —
  needs the same `eslint-disable-next-line react-hooks/set-state-in-effect`
  for the same hydration-mismatch reason) and `router.replace("/")`s a
  second same-session visit instead of re-rendering. No CTA button, no
  dashboard link, and no Profile menu entry to it — reachable only from the
  holdings onboarding Save and Skip flows (issue #280 moved the
  questionnaire's onboarding Save to `/holdings?onboarding=1`, so it is no
  longer a direct entry). Copy never claims a holdings-confirmation email
  was sent and never prints the current global MWF 17:00 schedule even
  though the user's own cadence is already `weekly` — that number is filled
  in only once a later cadence issue wires `weekly` into Beat. **Update
  (issue #280, 2026-08-31)**: when the receiving address (`delivery_email
  ?? email`, the same fallback the holdings line uses) has no verified
  timestamp, the delivery line claims send-stop instead of send — derived
  per scope exactly like the Profile page's issue #269 §6 rule (a set
  delivery_email is checked against `delivery_email_verified_at`, the
  account-email fallback against `email_verified_at`). **Update (issue
  #290, 2026-09-01)**: the #280 constraint ("must not claim reports won't
  be sent while #276 is open") is lifted — the welcome copy split into
  holdings status (never mentions send: `withHoldings` /
  `withoutHoldings`) and a delivery claim (`deliveryVerified`: "Reports
  will be sent to {deliveryEmail}." / `deliveryUnverified`: "Reports
  will not be sent until {deliveryEmail} is verified."). `welcome.emailUnverified` was
  removed; all three catalogs updated in lockstep (zh-Hant still gated
  out of the switcher). **Update (PR #294 review, 2026-09-01)**: the
  delivery claim mirrors Layer 2's send decision, not the #269 per-shown-
  address predicate — send-stop renders only when BOTH timestamps are
  null (the same condition as Profile's `noVerifiedRecipient` gap card);
  an unverified `delivery_email` does not block a verified account email,
  so that mixed state claims delivery to the account address instead.
  **Update
  (issue #280 item 3, 2026-08-31)**: successful login redirects
  unconditionally to `/profile` (was `/holdings`). `/login` only ever
  serves returning users — signup redirects straight to
  `/questionnaire?onboarding=1` and never passes through this action — so
  there is no new-vs-returning or onboarding-gap branch; interrupted
  onboarding is resumed from Profile's gap cards in edit mode. The
  pre-existing `/me` round-trip in `login/actions.ts` was removed with the
  branch.
- **Profile's gap card reads `GET /me`'s `missing` field** (`#220` shipped
  the full response shape already; this is the first UI consumer of
  `missing`/`has_questionnaire`/`has_holdings`). Renders nothing when
  `missing` is empty; one button per entry (`/questionnaire`, `/holdings`
  — **never** `?onboarding=1`, since that query string's only legitimate
  source is the post-signup redirect). This replaced two guard tests PR
  #228 had written to lock "Profile never renders a gap card, that's
  #221's job" — expected, since implementing #221 is what makes that
  boundary move.
- **Backend**: `report_cadence` now defaults to `"weekly"` at signup (was
  `"mwf"`); `users.tos_accepted_at` (already added in #220's migration) is
  now actually written, in the same transaction as the user insert. Admin
  manual-generate (`POST /admin/users/{id}/reports/generate`) dropped its
  no-holdings 422 — self-service `POST /reports/generate` never had it, so
  this closed a gap rather than opening one. `active_user_ids()` (scheduled
  fan-out) is untouched on purpose: it still requires a holding row, so a
  brand-new empty-book signup does not enter the still-global-MWF scheduled
  batch — that only changes once a cadence follow-up issue wires `weekly`
  into Beat and filters fan-out by `users.report_cadence`.
- **Fixed in passing**: adding `tos_accepted` as a required field exposed
  a real leak in `main.py`'s password-redaction handler for 422 bodies —
  pydantic v2's "missing field" validation error sets `input` to the
  *whole request body*, not just that field, so a sibling `password` value
  leaked in plaintext whenever a signup request failed on two fields at
  once (e.g. a valid password alongside an omitted `tos_accepted`). The
  handler now also scrubs known secret keys out of any dict-shaped `input`,
  not just errors whose `loc` mentions them directly.

### Global message catalog — supersedes the home-only locale gating above (issue #209, 2026-08-27)

The three bullets above ("`lang` attribute is route-scoped", "the locale
switcher itself is home-only", and the `npm run test` reference) describe the
2026-08-07 (#94) lightweight-i18n shortcut, now superseded. Current state:

- **One catalog, no more per-route text split.** `home-messages.ts`
  (`{en, zh}`) and `messages.ts` (English-only) are both deleted. Every
  in-product string lives in `frontend/src/locales/{en,zh-Hans,zh-Hant}.json`
  (see `frontend/src/locales/README.md` for the full mechanism), read via
  [next-intl](https://next-intl.dev)'s `useTranslations()` /
  `useHomeMessages()` (a thin `t.raw()` wrapper kept for `home-sections.tsx`'s
  existing object-access code shape). `zh` is renamed `zh-Hans`; `zh-Hant` is
  new (LLM-drafted, pending native-speaker review — see the README).
- **`lang` now always follows the selected locale, on every route, on the
  real `<html>` element** — the `pathname === "/"` gate described above is
  gone. `LocaleProvider` (not `AppShell`) owns this: it's the one place
  `locale` state changes (the storage restore and `setLocale`), so a
  `useEffect` there sets `document.documentElement.lang` directly. An
  earlier version of this PR only set `lang` on `AppShell`'s wrapper `<div>`
  — real, never on `<html>` itself, which `layout.tsx` still renders
  statically as `lang="en"` server-side (locale is client-only, see below) —
  caught by review (blacktomb42, PR #226) since screen readers and
  in-browser translate key off the real element. `AppShell` still also sets
  `lang` on its wrapper div (redundant with the fix, kept because
  `app-shell.test.tsx` already covered it and removing it added no value).
  `GetStartedMenu` and `SiteHeader` no longer branch on `isHome` for text
  either: that branch (home read `nav.*`, everywhere else read the
  English-only `menu.*`) was the root cause of the mixed-language menu bug
  (issue #207/PR #208 — Get Started/Login/Logout came from `home-messages`,
  Holdings came from `messages.ts`). One `menu` namespace, used identically
  everywhere, fixes it structurally rather than patching the specific label.
- **The locale switcher is no longer home-only** — it shows on every route,
  since every route now actually changes language when it's used. It also
  only ever offers reviewed locales: `zh-Hant` is excluded from `LOCALES`
  (`frontend/src/locales/index.ts`) until a native speaker signs off — see
  the locales README's "zh-Hant review status" (also fixed after the same
  review round: the catalog existed but was still switcher-selectable).
- **No URL-based locale routing.** Concept & Design's frontend engineering
  constraint 3 calls for `next-intl` with `/en`/`/zh-Hans`/`/zh-Hant` URL
  prefixes and SSR of the selected locale. Issue #209 explicitly required
  settling that question at implementation time rather than silently
  keeping the client-only shortcut — the product owner's call was to keep
  `localStorage` + client state, not add URL prefixes. Consequence: locale
  is only known client-side, so `/login`, `/signup`, and `/holdings` (all
  Server Components, for their own server-side data needs) each split their
  translated text into a small Client Component (`login-heading.tsx`,
  `signup-heading.tsx`, `holdings-heading.tsx`,
  `questionnaire-page-body.tsx`) — see `frontend/src/locales/README.md`'s
  "No URL-based locale routing" section for the full reasoning and the
  first-paint-flash tradeoff this accepts. Server Actions
  (`login/actions.ts`, `signup/actions.ts`) have no other way to know the
  visitor's locale either, so the client form submits it as a plain hidden
  field.
- **Structural lint**: `eslint-plugin-i18next`'s `no-literal-string` rule is
  wired into `eslint.config.mjs`, scoped to `src/app/**` and
  `src/components/**` (excluding tests) — a hardcoded user-facing string in
  JSX now fails lint instead of silently reintroducing what this issue just
  centralized.
- **Tests**: the test runner is `bun run test` (bun replaced npm as this
  project's package manager before this section was written — the `npm run
  test` reference above predates that). `site-header.test.tsx` /
  `get-started-menu.test.tsx` / `app-shell.test.tsx` still lock the
  auth-gated menu and chrome shape; they no longer parametrize by route for
  locale-visibility (there is nothing route-conditional left to test there).
  `frontend/src/locales/locales.test.ts` locks the three catalogs'
  structural shape in sync; `glossary-consistency.test.ts` locks the
  UI/report-glossary overlap terms (Custodian,
  `[Established]`/`[Probable]`/`[Speculative]`) against
  `backend/config/i18n_glossary.yml`.


