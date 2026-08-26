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
  fallback, on no deterministic schedule. A rapid multi-hop navigation
  (several link clicks within ~1s) collapses to one re-verify via a
  module-level grace-window timestamp, not one per hop. `getUser()` itself
  is bound by an 8s timeout + one retry (timeout only, not on an immediate
  network error) — `auth.portfonia.com` (the Caddy reverse-proxy routing
  around direct Supabase connectivity issues) has been observed spiking
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


