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
  sees only Log in; authed sees Home + Holdings + Questionnaire + email +
  Log out, in that order — issue #214 follow-up added Home as the first
  entry, an explicit way back to `/` from any inner page, on top of the
  brand-link click target). Home-only (`pathname === "/"`): the locale
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
  the same `markPendingLogin()` signal (post-signup also redirects to
  `/holdings`). `clearPendingLogin()` disarms it if login/signup returns
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


