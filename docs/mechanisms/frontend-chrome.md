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
  is in `PROTECTED_PATH_PREFIXES`, so an unauthenticated request redirects
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
- **Profile redesign (issue #269, 2026-08-30):** section order was gap
  card → Email Verification → Account → Investment style → Delivery email →
  placeholders → Change password → Delete account. **Issue #390 (layout
  only, 2026-09-09):** Investment style + Portfolio overview collapse into
  one **Holdings** card (four nav buttons: `/portfolio`,
  `/portfolio/performance`, `/holdings`, `/questionnaire`); Report
  language & currency + Report schedule + Report delivery email collapse
  into one **Report management** card (row 1: language/currency/cadence
  selects; row 2: delivery-email display/resend/fallback). Invite and delete stay unfinished placeholders; #597 makes cadence a
  subscription selector. Current order: gap
  card → Email Verification → Account → Holdings → Report management →
  Invite → Change password → Delete account. Issue #393 moved the
  inline Change password form to `/profile/change-password` (Altcha PoW
  on that page); `/profile` keeps only a link into it. Email Verification is
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
- **Change-password Server Action** (`app/profile/change-password/actions.ts`,
  issue #393) still follows the same `signInWithPassword`-then-`updateUser`
  pattern as `Ring 1-Profile Page.md` §三 decision 2 — verifies against
  the caller's own session email (`supabase.auth.getUser()`), never a
  client-submitted `email` form field, so a forged field can't steer whose
  password gets checked. Altcha PoW is verified against
  `POST /me/change-password/altcha-verify` first; missing/invalid PoW never
  reaches the Auth provider. `/profile` itself only links to that page.
- **Every non-implemented Profile section (delivery-email
  change, invite generation, delete account) is rendered with disabled
  controls**, never a submittable form — issue #220's requirement that
  these stay visible placeholders, not silently absent or falsely
  interactive. Portfolio overview shipped in issue #320/PR #322 — it is a
  real link into `/portfolio`, no longer in this placeholder set (see
  `docs/mechanisms/holdings-pipeline.md`'s C2 section). Issue #390 moved
  that link into the Holdings nav row (with Performance, Holdings
  management, and Investment style) and moved the cadence placeholder
  into Report management; #597 replaces the disabled cadence placeholder
  with the subscription selector below.

### Profile subscription controls and guidance (issue #597)

Profile reads `GET /me.subscription` and the two verification timestamps.
Report management offers Weekly, Mon/Wed/Fri and, only for an active plan
without pending cancellation, Cancel subscription. Without a verified address
it disables the selector but keeps the current type for an active subscription;
other states show a disabled No subscription placeholder. Selections never
optimistically change the displayed server state.

`use-subscription.ts` checks `next_adjustment_at` before any action and shows
remaining hours/minutes during the ET daily lock. It reads a quote before a
plan dialog (and the current-plan quote for cancellation's expiry). Dialog
amounts and dates come from that response, with the first-report instant
formatted in ET. An insufficient quote disables Confirm and directs the
user to credits in Account; Mon/Wed/Fri without holdings adds its warning.
All plan confirmations, including Account's Resume button, call
`setSubscription(type)`. The quote determines whether this is a no-charge
resume or a fresh charge after expiry. Cancellation calls `cancelSubscription()`.
The frontend does not call the backend resume endpoint. Five HTTP 409 codes
have translated messages; other failures use the generic error. Every successful
write closes its dialog and calls `router.refresh()`.

Account shows the subscription below Credits, with Cancel or Resume for an
active plan, an expired notice or a no-subscription notice otherwise. Notices
use the existing Badge. Welcome replaces the old weekly cadence sentence with
two plan/verification paragraphs and Choose a plan linking to `/profile`;
Portfolio remains the skip link. There is no selector on Welcome.

Successful email confirmation redirects from the server action to `/profile`.
The already-verified link state retains `emailVerification.successMessage`;
failed confirmation states are unchanged. Accepted limitations: ops-manual
verification also goes to Profile; a signed-out visitor reaches the existing
login redirect and, after login, Portfolio, with no return-path parameter.
The unsubscribe page explains stopped delivery, end-of-paid-period cancellation
when no verified address remains, no return, and no automatic resume after
re-verification. These controls and copy ship with #595/#596/#599.

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
  `PROTECTED_PATH_PREFIXES`, same as `/profile`/`/holdings` — no route-specific
  auth code needed). Server Component `page.tsx` calls `getMeServer()`;
  the Client Component `WelcomeBody` does a `sessionStorage.
  portfonia.welcomed` dedupe check in a `useEffect` (same one-time
  client-only-reveal pattern as `locale-provider.tsx`'s restore effect —
  needs the same `eslint-disable-next-line react-hooks/set-state-in-effect`
  for the same hydration-mismatch reason) and `router.replace("/portfolio")`s a
  second same-session visit instead of re-rendering. Under the guidance
  sit Portfolio (`/portfolio`, the `menu` label) and Choose a plan
  (`/profile`, #597); the load-error branch renders neither. There
  is no Profile menu entry to `/welcome` — reachable only from the
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
  (issue #280 item 3, 2026-08-31)**: successful login redirected
  unconditionally to `/profile` (was `/holdings`). `/login` only ever
  serves returning users — signup redirects straight to
  `/questionnaire?onboarding=1` and never passes through this action — so
  there is no new-vs-returning or onboarding-gap branch; interrupted
  onboarding is resumed from Profile's gap cards in edit mode. The
  pre-existing `/me` round-trip in `login/actions.ts` was removed with the
  branch. **Update (issue #586)**: that landing is `/portfolio`. `/welcome`
  shows Portfolio and Choose a plan (#597) above, and a same-session revisit
  replaces to `/portfolio`. The Portfolio page title row adds Edit holdings
  (`/holdings/edit`) and Investment style (`/questionnaire`, no query)
  beside View performance, reusing the existing `menu` labels. Edit-mode
  questionnaire Save and Skip still return to `/profile`.
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
  as `lang="en"` server-side at that time (issue #702 adds URL locales
  for public SEO pages, documented below) —
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
- **No URL-based locale routing (partially superseded by #702 for public
  SEO pages only; see below).** Concept & Design's frontend engineering
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

### LocaleSwitcher rebuild on MenuDropdown + flag-icons (issue #350 item 4)

`components/locale-switcher.tsx` was a plain native `<select>` — rebuilt on
the same `MenuDropdown`/Base UI `Menu` primitives `GetStartedMenu` already
used, for a consistent dropdown affordance across the header. Adds
`flag-icons` (MIT-licensed, SVG, ISO 3166-1-alpha-2 codes) as a per-locale
flag: English → `us`, Simplified Chinese → `cn`, Traditional Chinese →
`tw`. Same change lifted `zh-Hant`'s `UNREVIEWED_LOCALES` gate (see
"Global message catalog" above and `frontend/src/locales/README.md`'s
"zh-Hant review status" for that gate's full history) — the product owner
chose to ship the LLM-drafted catalog without native-speaker review rather
than wait, a deliberate logged decision, not an oversight. `currency-
switcher.tsx` (issue #354) was later rebuilt on the same primitives —
see `capture-and-reporting.md`'s per-pair FX entry.



### Reports menu and history (issue #642)

`AUTHED_ENTRIES` adds Reports (`FileText`, `/reports`) immediately after
Performance. Guests do not see it. Both report routes inherit the shared
header and existing auth gate. List filters and pagination live in the URL;
only available rows link to detail. The detail downloads Markdown and opens
browser print for PDF. Report print styles hide the shared header/menu and
controls without changing their screen presentation. All new copy is in the
`reports` namespace and `menu.reports` across en, zh-Hans and zh-Hant.
Issue #660: the Chinese `menu.reports` and `reports.title` read "Report Center"
in four characters, matching the other menu entries. `/reports` and `/agent`
use the `/portfolio` typography: `font-heading text-2xl font-medium` page
heading, `text-sm` body, muted secondary text, and `/agent` sections in `Card`.

### Download and send confirmation; 375px fixes (issue #679)

Every user download is fetched first and saved only after confirmation:
Portfolio .xlsx/.md, Holdings export, Holdings template, and the report-detail
Markdown. The caller keeps a `PendingDownload` (`blob`, `filename`,
`description`) and renders the shared `DownloadConfirmDialog`
(`components/download-confirm-dialog.tsx`), which shows the description, the
file name and `formatFileSize(blob.size)` (bytes below 1 KB, else KB with one
decimal) and calls `downloadFile` on Confirm. The dialog opens after the fetch
because server file names come from `Content-Disposition`; a failed fetch
keeps each caller's existing error handling and opens no dialog. Report print
is excluded (the browser print dialog is the confirmation).
`SendOverviewButton` opens its own confirmation naming the base currency and
"your report delivery address" (the recipient is resolved server-side);
Confirm runs the unchanged send/cooldown logic. Copy lives in the new
`downloadConfirm` namespace plus per-caller `exportDescription`,
`templateDescription`, `downloadDescription` and `sendOverviewConfirm*` keys.

375px acceptance (production measurement, 2026-10-06): the `/portfolio`
header now puts the currency switcher on row 1 and the send/download buttons
on a wrapping, right-aligned row 2 at every width. `MultiSelectMenu` and
`BenchmarkSingleSelectMenu` pass `max-w-full whitespace-normal text-left` to
`MenuDropdown` so a long trigger wraps instead of being clipped by its card;
the shared `MenuDropdown` default is unchanged. The Performance monthly card
header and the Holdings "current holdings" action row wrap.

## Public SEO pages and locale URLs

Issue #702 adds English (unprefixed), `/zh-Hans`, and `/zh-Hant` versions of
`/`, `/about`, `/pricing`, `/waitlist`, `/privacy`, `/terms`, and `/refund`.
`lib/seo.ts` defines this exact page set, canonical `https://portfonia.com`,
URL helpers, protected prefixes, language alternates, and metadata builders.
It is safe for client imports. Only `lib/seo-server.ts` reads `next/headers`;
the root layout and SEO pages use it to resolve the proxy locale header.

Proxy removes client-supplied locale headers on every request and constructs
forwarded headers after session refresh, retaining refreshed request cookies.
It sets `en` on unprefixed SEO paths, Chinese on prefixed paths, and no locale
header on other unprefixed paths. All header mutations precede response
construction, including RSC flight and waitlist server-action requests.
Proxy rewrites Chinese SEO URLs to the unprefixed route and forwards
`x-portfonia-locale`. Chinese unknown paths also forward that header without
rewriting or redirecting. Both branches preserve refreshed session cookies
and the cache-prevention headers supplied by Supabase. Only `/holdings`,
`/portfolio`, `/profile`, `/questionnaire`, `/reports`, `/welcome`, and their
subpaths redirect anonymous visitors to `/login`; unknown URLs reach the
localized Next 404. Protected-path checks decode once and collapse repeated
slashes, match whole path segments, and do not resolve decoded dot segments.
Malformed escapes remain unprotected for Next to handle. The existing `/api/` bearer injection is unchanged.

The async root layout renders the URL locale in `<html lang>` and initializes
`LocaleProvider` with it. Reading headers makes page rendering dynamic.
Unprefixed SEO pages always display English without reading or overwriting
a stored Chinese preference. Chinese route mounts persist their locale.
App/auth pages retain #209's English-first storage restore.
Public-page links preserve locale. A visible, server-rendered `LanguageLinks`
row ends all seven SEO pages; its current locale is unlinked and marked
`aria-current="page"`. Cross-locale links use native anchors. The header
switcher also uses native anchors on SEO pages, preserving query and hash,
and retains buttons on other pages. Anchor clicks persist the selected locale
through `rememberLocale`, without navigation or preventing the full document
load. `setLocale` also changes only state and storage. After mount, pathname
changes, `popstate` and `routeLocale` prop changes synchronize SEO-page state
from `window.location.pathname`; non-SEO pages restore the stored preference.
The document-language effect depends on both locale and route locale. The
shared segment-cache keys remain safe while pages render dynamically with
`staleTimes.dynamic = 0` and without `cacheComponents`/PPR.

Each SEO page emits catalog title/description, absolute canonical and ten
language alternates (`en`, `zh-Hans`, `zh-CN`, `zh-SG`, `zh`, `zh-Hant`, `zh-TW`, `zh-HK`, `zh-MO`,
`x-default`), complete Open Graph metadata and a large-image Twitter card.
`robots.ts` allows public URLs, disallows protected routes, `/api/` and `/agent/revoke` and names the
sitemap. `sitemap.ts` lists 21 localized URLs with the same alternates and no
invented modification dates. The six auth-flow pages remain crawlable but
export shared `NOINDEX_METADATA` (`index: false`, `follow: true`), with no
canonical/hreflang and no sitemap entries. The home page contains WebSite,
Organization and
SoftwareApplication JSON-LD, with `<` escaped as literal `\u003c`, without
prices, ratings or reviews.

`/og/en`, `/og/zh-Hans` and `/og/zh-Hant` are static 1200x630 PNG route outputs.
Issue #706 shows holdings ingestion and market/macro tracking inputs, a gold
brace, and sample briefing sections with confidence labels from existing
catalog fields, never user data. At build time the font loader makes three
Google Fonts glyph-subset requests per locale (sans 400/600, serif 400) with
Next's bundled OG legacy User-Agent, accepts only OpenType/TrueType CSS sources, and fails on
unsupported formats or failed requests. The home preview shows that locale's
image with localized alt text above the unchanged HTML sample report.
The home performance sample uses real 2026 S&P 500 and CSI 300 paths with an
illustrative portfolio line; the data is fixed in the source.
`/about` draws its five sections solely from facts already in the English
catalog; all new text is translated in the three catalogs.

Caddy redirects `www.portfonia.com` permanently to the apex, preserving path
and query. A host-only session on `www` requires a fresh apex sign-in.
Deployment includes frontend rebuild and Caddy reload under separate owner
authorization. Unit tests cover routing, cookie/header preservation, locale
state, metadata, sitemap, content and font failures; build verifies the client/
server boundary and emits the PNGs. No local app server is used. HTTP status
and canonical-host behavior are verified by the owner after deployment.


## Jade page, Profile coordination and menu (issue #710)

Every authenticated menu includes Jade directly after Performance; guests do
not see it. `/jade` joins the protected app paths, inherits the shared header,
and is absent from public SEO pages and the sitemap. The server page loads
`getMeServer`, preserves redirect errors and displays the existing Profile load
error on other failures. All authenticated users can open the introduction and
subscription entry; future risk tools are absent.

The Jade client reuses Profile's `useSubscription` and `SubscriptionDialog`.
Inactive/cancelled/Expired or briefing-plan users see Subscribe to Jade; no
verified recipient disables it. Active Jade shows its paid period and Cancel,
or Ends and Resume when cancellation is pending. A Jade subscribe/change quote
also states its resulting translated cadence. Successful subscription actions
refresh the server page and re-probe session flags through the shared hook.

Only active Jade shows its Weekly, Mon/Wed/Fri and Daily schedule select. A
change disables the control, calls `setJadeCadence` without a dialog or daily
lock, then refreshes. Failure retains the previous value with localized error
copy. Selecting Daily/Mon/Wed/Fri without holdings shows the existing warning.
Profile displays Jade's plan label but replaces Cancel/Resume with `/jade`
management links; its disabled plan selector displays the cadence and never
adds a Jade option. Expired Jade retains ordinary Profile controls.

The menu uses session flags: `jade` takes precedence over Advanced gold.
`.jade-surface` uses `#2f8a5f` and light text `#f4fbf6` in both themes. Its inline
SVG fractal-noise layer is screen-blended over the flat base, which remains if
the image fails. The overlay cannot intercept clicks. Frontend subscription
status/type checks select management controls only; backend helpers own access.

The Chinese Profile page name changes in navigation, page headings, return
links, unsubscribe page references, Privacy's account-deletion clause and four
subscription notices. Privacy's personal-data uses are preserved. Pricing,
Terms, the home FAQ, Welcome and agent endpoint descriptions include Jade;
Terms separately describes unlimited Jade cadence changes and management on
Jade. All three catalogs retain matching key sets and hand-authored Chinese.

375px component checks cover the full-width schedule selects, wrapping action
rows and both-theme Jade trigger class; they do not constitute physical-device
or browser layout measurements. The Jade page uses `max-w-2xl px-4 sm:px-6`.
The production frontend build is a required gate. No local app server is run.

## Calculating overlay (issue #716)

`frontend/src/components/calculating-overlay.tsx` exports `CalculatingOverlay`
with `active`, caller-translated `label`, `children`, and optional `className`.
It contains no domain imports or catalog lookups. Its relative, min-width-zero
wrapper marks active work with `aria-busy`; the child wrapper is inert while
active. An absolute full-region pointer layer shows a spinner and polite status
label over a translucent dark-theme surface. Inactive children remain interactive.

Jade replay uses it around the complete settings/results/method region, which
renders immediately even without a response. The intro and error alert sit
outside. Span buttons wrap at 375px; the overlay follows the region's height.
The label is `common.calculating` in all three catalogs. The component is intended
for reuse by Performance later; Performance is unchanged in this issue.
