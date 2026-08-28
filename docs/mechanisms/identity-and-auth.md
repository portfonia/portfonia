# Ring 1 stage B: analysis framework, identity seam, users/auth, frontend auth closure

### System default analysis framework — B1 (Ring 1 stage B, issue #129, PR #172)

The system-wide "house analytical stance" from `Portfonia Concept & Design.md`
§4.3 ("System Default Investment Philosophy") had sat as unimplemented prose since 2026-05-14 — the
real system prompt (`_COMPLIANCE_SYSTEM_PREFIX` + `_SHARED_BODY_RULES`) never
carried any product-specific investment philosophy, only compliance
constraints and structural writing rules. B1 closes that gap.

- **`config/analysis_framework.yml`** (`app/services/analysis_framework.py`
  loader) — English-only prose (reasoning layer, never surfaced to the
  reader), **hot-reloaded on every call** (same contract as
  `asset_class_config.load_asset_class_config`: an edit takes effect on the
  next report, no process restart), fails loudly on a missing file or an
  empty/missing `version`/`text` rather than silently degrading to a
  neutral framework. `version` is written to
  `report_inputs.analysis_framework_version` on every report (audit only —
  the full text is deliberately never stored, to keep it out of any future
  endpoint that reads `report_inputs`). Bilingual review record + the
  product owner's sign-off: Obsidian `Hermes/Portfonia/Analysis Framework
  Philosophy.md`.
- **Injection order is compliance -> framework -> shared body rules**, each
  layer explicitly subordinate to the one before it — `_build_pass2_system()`
  / `_build_assembly_system()` (`report_prompts.py` / `report_assembly.py`)
  compose this fresh on every call. These became **functions, not module
  constants** — the pre-B1 `_PASS2_SYSTEM`/`_ASSEMBLY_SYSTEM` were frozen at
  import time, which would never pick up a config edit in a long-lived
  Celery worker process. Both call the same `load_analysis_framework()` —
  one philosophy, not two hand-copied texts (this module has twice paid for
  that class of drift: PR #117's two CSS strings, PR #157's two
  `_FORWARD_WINDOW_DAYS`).
- **The framework only reallocates attention, never produces a directional
  claim** — it decides what earns space and how a time horizon frames
  significance; a self-limiting clause makes it explicitly subordinate to
  every evidence/directional-claim rule below it in the same prompt. Eight
  numbered items (time horizon, structural-evidence depth, portfolio-shape
  weighting, macro/geopolitical transmission, relevance-not-prevalence,
  condition-change-without-forecast, valuation-as-documented-relationship,
  trace-to-a-named-observable) plus that clause — full text and the
  Concept §4.3 mapping: Obsidian doc above.
- **`v1` -> `v2`** (2026-08-22, same PR cycle): after a real-report overlay
  comparison the product owner tightened items 1/2/3/8 — explicit defaults
  for session-scale price moves, a "trace to structural-position change"
  requirement for item 2, an explicit weight/evidence rebalance self-check
  for item 3, and named example phrases for item 8's "no generic closer"
  rule ("worth watching" etc. banned only standing alone). **A code-level
  automated version of the item-3 weight/evidence check (parse §3, score
  evidence strength, reject-and-retry) was explicitly deferred** — no
  evidence-strength scoring mechanism exists yet; tracked in issue #173.
- **§2 Macro Signals rewrite** (same PR, product owner's explicit ask):
  `_SECTION2_INSTRUCTIONS` (`report_prompts.py`) and the inline §2 block in
  `report_assembly.py` changed from "cover every triggered macro theme
  under a rigid bold 'Impact on this portfolio' sub-heading with forced
  short/medium/long-term sub-bullets" to "select 2-4 themes with genuine
  evidenced change this period, write each as one flowing paragraph, let
  the analysis framework's own judgment decide space/time-horizon framing".
  A theme with no direct, concrete mapping to a held identifier does not
  earn its own §2 paragraph by default — at most an aside inside the
  relevant holding's §3 analysis. **Cross-report repetition avoidance is
  prompt-only today** ("don't restate at the same length report after
  report") — there is no persisted memory of what a previous report
  covered; a real fix needs a ledger analogous to `news_surfaced` (see
  below), tracked in issue #171.
- **`_PROMPT_VERSION` -> `f2-v7`** (`report_generator.py`) — the bump
  comment also documents that `f2-v6` was itself under-documented (PR #168's
  narrative-layer rewrite changed `_SHARED_BODY_RULES` without bumping this
  constant, the same class of gap PR #167 round 3 caught on
  `ASSEMBLY_PROMPT_VERSION`).
- **Provenance**: two rounds of independent code review (blacktomb42) —
  round 1 found 2 real bugs (`ASSEMBLY_PROMPT_VERSION` not bumped for a real
  contract change; the widened tech-breakthrough theme's bare `breakthrough`
  keyword and its Chinese-language counterpart firing on ordinary headlines),
  round 2 (after fixes) found 0 bugs.
  Merged squash `8287dd3`. Deployed to production 2026-08-22.


### Identity seam: current_principal + explicit user_id — B3 (Ring 1 stage B, issue #129, PR #181)

Before B3, "who is calling" was resolved three different ways, all
ambient: a bare `get_current_user_id()` call inside `routers/reports.py`'s
three read endpoints (invisible to FastAPI's `dependency_overrides`, since
that only intercepts `Depends(...)`, not a direct function call); a
`user_id: UUID | None = None` fallback inside `generate_report`/
`regenerate_report` that silently resolved to the dev user if a caller
forgot to pass one; and `email_sender.send_report_email` hardcoding every
recipient to `settings.DEV_USER_EMAIL` regardless of whose report it was.
B3 collapses this into two explicit channels so a later auth swap cannot
silently miss a call site. B4 filled `current_principal` with JWKS
verification (next section); the seam itself is unchanged.

- **`Principal` + `current_principal(request)`** (`app/core/deps.py`) is the
  one request-scoped identity entry point. Every identity-bearing route
  across `/reports/*`, `/holdings/*`, and `/portfolio/*` depends on it via
  `Depends(current_principal)` — B4 filled this function's body with JWKS
  verification; no call site changes. Locked by a structural test
  (`test_every_identity_bearing_route_depends_on_current_principal` in
  `test_identity_seam.py`) that iterates `app.routes`, added in review round
  1 after `holdings.py`/`portfolio.py`'s 6 endpoints were found still wired
  to the lower-level `Depends(get_current_user_id)` — that would have been
  a split-identity trap for B4: a JWT swap landing only in
  `current_principal` would leave those 6 routes serving `DEV_USER_ID`
  forever.
- **`generate_report`/`regenerate_report` require `user_id`, no fallback.**
  A structural test bans `get_current_user_id`/`DEV_USER_ID` from
  `app/services/**` and `app/tasks/**` entirely. B3's one documented
  exception was `app/services/user_directory.py`'s `recipient_email` shim
  (`DEV_USER_ID` → `DEV_USER_EMAIL`); B4 replaced that body with a `users`
  table lookup (same signature) and the `DEV_USER_ID` ban has no remaining
  exception.
- **`send_report_email` fails closed on an unresolved recipient**: no send,
  an ops alert, `email_sent_at` stays null — never falls back to
  `ADMIN_EMAIL` or any other default. A report belongs to a specific user;
  routing it anywhere else is still a leak, and one that would read as
  "delivered" in the logs, permanently masking the bug. Caller-side logging
  in `report_generator.py` was reworded from "email sent but state
  unconfirmed" to "email delivery not confirmed" (review round 1) — the old
  wording was accurate only for the pre-existing commit-failure case and
  became misleading once `False` also meant "recipient never resolved".
- **Provenance**: one round of independent code review (blacktomb42) — 0
  bugs, 3 suggestions (the `holdings.py`/`portfolio.py` split-identity gap
  above; the misleading email-sent log; missing cross-user 404 coverage on
  the `regenerate`/`send` write paths, closed with tests exercising the
  real functions rather than mocks) + 1 nit (the sparse-history backfill
  log line dropped the leaked ticker list but still had no `user_id`
  attribution — fixed by threading it through
  `_tickers_with_sparse_history`, which stays a global query; only the log
  line is per-user). 0 bugs did not trigger a second review round per this
  repo's standing convention. 957 tests passing. Merged squash `a06fc9c`.
  **Superseded by issue #194 / PR #197 (2026-08-25):** the sparse *check* is
  now this user's auto tickers; `price_snapshots` remains a global store.


### Users, invites, and JWKS auth — B4 (Ring 1 stage B, issue #129, PR #183)

B4 is the identity *source* behind the B3 seam. `current_principal` reads
`Authorization: Bearer`, verifies it locally against
`{SUPABASE_URL}/auth/v1/.well-known/jwks.json` (ES256/RS256 only;
`aud=authenticated`, `role=authenticated`), then looks up
`users.auth_subject` with `status == "active"`. **Settings has no
`JWT_SECRET`.** New hosted-Auth projects default to asymmetric JWKS, not
HS256; do not add a shared signing secret.

Dashboard names (2026): Publishable key → `SUPABASE_ANON_KEY` (alias
`SUPABASE_PUBLISHABLE_KEY`); Secret key → `SUPABASE_SERVICE_ROLE_KEY`
(alias `SUPABASE_SECRET_KEY`). An opaque `sb_secret_…` key is not a JWT —
admin HTTP calls send it on the `apikey` header only, never
`Authorization: Bearer` (`Invalid JWT`). Do not store the JWT signing
secret or the Supabase database password (business Postgres is self-hosted).

- **No auto-insert.** A valid token whose `sub` has no `users` row is 401.
  `get_current_user_id()` raises (`use Depends(current_principal)`). Until
  B5, identity is Bearer-only — do not parse a session cookie here.
- **`users` PK is ours**, not the Auth `sub`. Invite redeem is atomic
  (`UPDATE … WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at > now() RETURNING`).
  `POST /auth/signup` is backend-mediated; after Auth create succeeds, any
  later failure calls `delete_auth_user`.
- **Seed bind** (ops token): `POST /admin/users/{id}/bind-subject`. Sets
  `auth_subject` only when it is still NULL. 409 if this row is already
  bound **or** another row already holds that `sub`; 422 for whitespace-only
  input. The B4 migration leaves the production seed row's `auth_subject`
  NULL on purpose.
- **Ops hard-purge** (issue #199): `DELETE /admin/users/{id}?confirm={email}` removes the `users` row and that user's own data. The hosted Auth account is **not** deleted — the operator deletes it in the Supabase Dashboard. After a successful purge the Auth `sub` is an orphan; a later signup with the same email can insert a new `users` row (email unique is free once our row is gone) but Auth may still reject "user already registered". This endpoint does not sequence Auth deletion. Soft-delete via `users.status = "deleted"` is unused here.
- **`recipient_email(session, user_id)`** reads `users` (`delivery_email`
  else `email`); missing or non-`active` → `None`. Send stays fail-closed.
- **Invite creation checks `users.email` overlap** (issue #188, PR #219):
  email-bound `POST /admin/invites` → **409** when `users.email` already
  holds the normalized address (strip + lowercase; **no status filter** —
  the same predicate as `POST /auth/signup`). Both call sites share one
  lookup, `signup_email_taken` in `app/services/invites.py`; do not fork
  the query. Generic (`email` omitted) invites are unchanged. Redeem stays
  undistinguishable `InviteRejected`. Accepted race: a concurrent signup
  between check and commit still wins; `uq_users_email` is the backstop.
- **`active_user_ids`** is sourced from `users.status == "active"` but
  requires `EXISTS` a holding row — a fresh signup is not fanned out on the
  next M/W/F batch.
- **Public 422 must not echo `password`.** `SignupRequest.password` is
  `SecretStr`; Pydantic still puts the raw string in `"input"`. `main.py`
  redacts `password` fields on `RequestValidationError` after
  `jsonable_encoder(exc.errors())` so `ctx` stays JSON-serializable.
- **Caddy** reverse-proxies `auth.portfonia.com` to `SUPABASE_PROJECT_HOST`.
  Compose interpolation is `${SUPABASE_PROJECT_HOST:?required}` (fail-closed
  empty default).
- **Do not deploy B4 without B5.** Unauthenticated calls to `/holdings`,
  `/reports`, and `/portfolio` now 401. Before production: re-run
  `SELECT DISTINCT user_id` on the four tables (Ring 1-B design.md §6.7);
  put `SUPABASE_URL`, the two keys, and `SUPABASE_PROJECT_HOST` in the
  server `.env` (fresh values, never copied from `.env.local`); point DNS
  for `auth.portfonia.com`.
- **Provenance**: three independent review rounds (blacktomb42). Round 1
  Request changes (signup compensation too narrow, plus bind-subject /
  empty-book fan-out / JWKS network→401 / Bearer-only / CHECK names).
  Round 2 Request changes (bind-subject unique `IntegrityError` → 500;
  empty Caddy host; 422 echoed password — `SecretStr` alone does not strip
  `"input"`). Round 3 Approve on `63a9023`. Merged squash `38afc68`
  (2026-08-24). **Deployed to production together with B5 on 2026-08-25**
  (see the B5 entry below for the deploy/UAT record).


### Idle-timeout server enforcement — issue #235 (2026-08-27)

**Bug**: issue #207/PR #208 (2026-08-25) implemented the product spec's
"15 minutes of inactivity auto-logout" entirely client-side
(`frontend/src/hooks/use-idle-logout.ts`, `frontend/src/lib/idle-timeout.ts`)
— the activity timestamp lived only in a React `useRef`. Closing the
tab/browser destroys that timer along with it; nothing fires a logout and
nothing persists to resume the count on reopen. The result: close the
browser in the morning, reopen it that evening, still logged in. The
implementing session's own code comment
(`use-idle-logout.ts:7-14`) already named this limitation, and it made it
into the design doc's §5 "honest limitations" section, but was never
escalated to a standalone decision point (the OQ-1..OQ-5 pattern used
elsewhere in that doc) for explicit product-owner sign-off — see the
`feedback_scope_narrowing_needs_explicit_decision_point` memory for the
general process note this incident produced.

**Fix — server-side idle enforcement, folded into B4's choke point**:
`app/core/idle_activity.py` stores a per-user last-active epoch timestamp
in Redis (`session:active:{user_id}`), and `current_principal`
(`app/core/deps.py`) checks it after JWT verification and the `users` row
lookup, before returning a `Principal`: `is_idle(user.id)` → 401 if the
stored timestamp is more than `IDLE_TIMEOUT_SECONDS` (900s, matching the
frontend's `SESSION_IDLE_TIMEOUT_MS` — kept in sync by hand, no shared
config crosses the Python/TypeScript boundary) old; otherwise
`touch_activity(user.id)` resets the window. Because this lives inside
`current_principal`, the existing structural test
(`test_identity_seam.py::test_every_identity_bearing_route_depends_on_current_principal`)
already guarantees every identity-bearing route gets idle enforcement for
free — no per-router wiring needed.

- **Timestamp comparison, not key expiry, is the enforcement mechanism.**
  The Redis key's own TTL (`_GC_TTL_SECONDS`, 24h) is a garbage-collection
  safety net only, deliberately far longer than the 15-minute policy.
  Redis cannot distinguish "key never existed" from "key expired" —  both
  read as absence — so if the 900s idle window were enforced by key
  expiry, a fresh login (no key yet) and a genuinely-idle session (key
  fell out) would be indistinguishable, and the fresh-login case has to
  read as *not* idle. Storing the actual timestamp and comparing in
  application code sidesteps this: "no timestamp" always means "never
  active," which is always safe to treat as not-idle, regardless of why
  the key is absent.
- **Fail-open on Redis outage, not fail-closed** — a deliberate departure
  from `app/core/rate_limit.py`'s fail-closed (503) convention for
  security-relevant checks. The reason is blast radius: `current_principal`
  is the single choke point for essentially every authenticated route in
  the app, unlike the rate limiter (scoped to signup/invite paths only).
  Idle-timeout is defense-in-depth layered on top of JWT verification,
  which stays the primary, fail-closed auth boundary — a Redis blip taking
  down the *entire app* for a control that, before this fix, provided zero
  enforcement at all would be a worse regression than temporarily reverting
  to that pre-fix (fail-open) state. Both `is_idle` and `touch_activity`
  catch `ActivityStoreUnavailable`, log, and continue.
- **Absolute session lifetime (hard cap regardless of activity) is
  explicitly out of scope for this fix.** Confirmed 2026-08-27: Supabase
  Dashboard → Authentication → Sessions shows "Time-box user sessions" and
  "Inactivity timeout" both set to 0 (never) — and both are Pro-tier
  settings. The Portfonia Supabase project is on the **Free plan**, so
  neither is actually usable regardless of what value is entered; an
  absolute-lifetime cap will need the same app-level treatment as this
  fix (a session-start timestamp checked in `current_principal`,
  independent of `idle_activity.py`'s rolling last-active timestamp), not
  a Supabase-native setting. Tracked in a separate follow-up issue.
- **Per-user configurable session length remains B6 scope**, per
  product-owner decision 2026-08-27 — not pulled forward into this fix.
- Tests: `app/tests/test_idle_activity.py` (unit — swappable
  `InMemoryBackend`, matching `rate_limit.py`'s test pattern) and two
  additions to `app/tests/test_auth_deps.py`
  (`test_active_session_within_idle_window_stays_authenticated`,
  `test_idle_session_beyond_window_is_401`) exercising the real HTTP path
  through `current_principal` with `time.time()` monkeypatched. A new
  autouse fixture, `_idle_activity_memory` in `conftest.py`, gives every
  test a fresh in-memory store — mirrors `_rate_limit_memory`.


### Frontend auth closure — B5 (Ring 1 stage B, issue #129)

Closes the loop B4 opened: `current_principal` (B4) requires a Bearer JWT on
every non-public backend route, but until this checkpoint nothing in the
frontend could produce one — every route was unconditionally public. B5
adds `/login` + `/signup?invite=<token>`, a cookie-based session via
`@supabase/ssr`, and credential forwarding on the three paths the frontend
uses to reach the backend (§2.6 of the design doc). **B4 must not deploy
without B5 in the same release** — see the B4 section above.

- **`src/proxy.ts`, not `src/middleware.ts`.** Next.js 16 (the version this
  repo runs) renamed the `middleware.ts` file convention to `proxy.ts` —
  same function, `export function proxy(request)` instead of `middleware`
  (confirmed against `node_modules/next/dist/docs/.../file-conventions/
  proxy.md`, not assumed from training data — see `frontend/AGENTS.md`'s
  standing warning to check the vendored docs before writing Next.js code
  in this repo). Do not "fix" this back to `middleware.ts`.
- **Session shape: cookie, not `localStorage`** — `@supabase/ssr`'s
  `createServerClient`/`createBrowserClient`, cookie-adapter pattern taken
  verbatim from Supabase's own Next.js-16-specific AI-integration guide
  (`getAll`/`setAll`, never the deprecated per-cookie `get`/`set`/`remove`
  shape). Reason stays what the design doc gave: a Server Component reading
  `listHoldingsServer()` has no access to `localStorage` at all. **CSRF**:
  rests on the same-origin `/api` rewrite plus `@supabase/ssr`'s
  library-default `SameSite=Lax` (also `httpOnly: false`, required so the
  browser `AuthStatus` client can read the session) — FastAPI's CORS is not
  on the user-browser path at all (§7.3(5) above), so it isn't part of this
  story either way. Do not switch these cookies to `SameSite=None`.
- **Backend stays Bearer-only** (`current_principal` was never touched this
  checkpoint) — the frontend's job is turning "there is a valid session
  cookie" into `Authorization: Bearer <access_token>` on every path that
  reaches the backend. Three paths, three different mechanisms, each with
  its own test (design doc §7.3(1) called this the easiest thing to miss):
  - **Same-origin `/api/*` rewrite** (`lib/api.ts`, unchanged): a browser
    `fetch("/api/...")` goes straight through `next.config.ts`'s
    declarative rewrite with no Node code in between, so there is nowhere
    else to attach a header — `proxy.ts` derives the token from the
    (refreshed) session cookie and injects it via
    `NextResponse.next({request:{headers}})` before the rewrite fires
    (Proxy runs before rewrites in Next's execution order). Verified this
    mechanism is real in this Next version via
    `node_modules/next/dist/server/web/adapter.js`'s
    `x-middleware-request-*` convention, not assumed.
  - **`app/api/holdings/upload/route.ts`**: has its own filesystem route,
    which wins over the declarative rewrite for this one path, so it never
    sees proxy's injected header on its own outbound `fetch()` — it derives
    the token itself via `lib/supabase/server.ts`'s `currentAccessToken()`.
  - **`lib/server-api.ts`'s `listHoldingsServer()`** (SSR direct read): same
    reasoning, same `currentAccessToken()` call — Server Components never
    automatically inherit a browser's same-origin cookie forwarding.
  - Deliberately NOT relying on proxy's header propagation for the latter
    two, even though it might work — Next's own authentication guide is
    explicit that Proxy must never be the only line of defense; each path
    verifies its own credential independently.
- **`lib/supabase/server.ts`'s `currentAccessToken()` reads `getSession()`,
  not `getUser()`** — deliberate: `getUser()` makes a network round-trip to
  re-verify against the Auth provider on every call, which is redundant
  here since the backend's `current_principal` independently re-verifies
  the JWT via JWKS anyway (B4). `proxy.ts` is the one place that DOES call
  `getUser()` — that's what actually triggers a refresh of an expiring
  token and rewrites the session cookie.
- **`/auth/signup` (B4) does not itself issue a session** — its response
  schema (`SignupResponse`) is just `{id, email}`. `app/signup/actions.ts`
  calls the backend to redeem the invite and create the account, then
  immediately calls `supabase.auth.signInWithPassword()` with the same
  credentials so sign-up is one step, not two. This wasn't specified in the
  design doc (which only fixed the registration mechanism, not the
  post-signup UX) — noted here as an implementation decision, not a design
  deviation.
- **Route protection is optimistic only, matching Next's own guidance**:
  `proxy.ts` redirects an unauthenticated request to a non-public page to
  `/login`, but every `/api/*` path is exempted from the redirect (a
  redirect would be nonsensical for a `fetch()`-consuming client — the
  backend's own 401 is what `lib/api.ts`'s `ApiError` already handles). The
  real, non-bypassable boundary stays `current_principal` on the backend.
- **CORS left unchanged, not tightened further** — already scoped to
  `allow_origins=[FRONTEND_URL]` (not a wildcard) before this checkpoint,
  and since every browser-originated user-facing call already went through
  the same-origin `/api/*` path (`lib/api.ts` never called `api.portfonia.com`
  directly), there was nothing cross-origin left to tighten. The direct
  face (`api.portfonia.com`) remains reserved for `/admin/*` (bearer-token
  tooling, not browser+CORS — B2) and `/health`, per decision point 11.
- **`NEXT_PUBLIC_SUPABASE_URL` (frontend build arg) is deliberately NOT the
  same value as the backend's `SUPABASE_URL` Setting** — the backend talks
  to the raw Supabase project host directly (JWKS verification happens on
  our own server, no reachability concern, B4 §6.5 point 2); the browser
  must go through the `auth.portfonia.com` Caddy reverse proxy (mainland-
  reachability workaround, B4 §2.7/§6.10). Getting these swapped would
  silently break login for exactly the users the proxy exists for. Wired
  as new `frontend/Dockerfile` build args (mirroring the existing
  `BACKEND_URL` pattern) and `docker-compose.yml`'s `frontend.build.args`
  (`NEXT_PUBLIC_SUPABASE_ANON_KEY` reuses `SUPABASE_ANON_KEY` — same
  publishable key, safe to expose to the browser by definition;
  `NEXT_PUBLIC_SUPABASE_URL` defaults to `https://auth.portfonia.com`,
  overridable via `SUPABASE_PUBLIC_AUTH_URL`).
- **`messages.ts` still has no `zh` map** (pre-existing gap, issue tracked
  separately) — `/login` and `/signup` render `lang="en"` like every other
  non-home route (`AppShell`'s existing route-conditional `lang`, unchanged
  by this checkpoint). `SiteHeader`'s new login/logout entry follows the
  same split already established for the Holdings link: locale-aware
  `home-messages.ts` strings on `/`, English-only `messages.ts` strings
  everywhere else.
- **Verified real `docker compose build frontend`** per the Quality Gates
  addendum this checkpoint itself triggers (touches `frontend/Dockerfile`
  and `docker-compose.yml`).
- **Merged and deployed to production, 2026-08-25.** PR #185 squash-merged
  `e57b5e1` (2026-08-24) after three independent review rounds
  (blacktomb42) — round 1: 1 bug (dropped `@supabase/ssr` `setAll`
  cache-prevention headers) + 2 suggestions + 1 nit; round 2: 1 bug (the
  round-1 fix's blanket header copy clobbered Next's own
  `x-middleware-override-headers` bookkeeping header, silently dropping the
  Authorization override even though the header *value* stayed present —
  the round-1 regression test was false-green, checking only that the
  value existed) — both bugs verified by reproducing them with a failing
  test before fixing, not accepted on the reviewer's word; round 3: 0 bugs,
  1 suggestion (derive the cache-prevention header set live from
  `setAll`'s own argument instead of a hardcoded copy — closes the exact
  drift class round 1's fix risked) + 1 nit.
- **Deployment record**: B4+B5 were deployed together on 2026-08-25, per
  the B4 section's "do not deploy B4 without B5" rule — production had
  been sitting on the B2 commit (`b4f51c4`) until then. §6.7's four-table
  `SELECT DISTINCT user_id` re-check, the server `.env` Supabase vars
  (fresh values, never copied from `.env.local`), and the
  `auth.portfonia.com` DNS record were all put in place ahead of the
  deploy per the B4 section's checklist. Production completed **two
  rounds** of end-to-end UAT (design doc §10.3, script: Obsidian
  `Hermes/Portfonia/Docs/Ring 1-B5 UAT script.md`, execution record there)
  — see `Hermes/Portfonia/Docs/Ring 1-B design.md`'s status line for the
  authoritative confirmation that B1-B5 are implemented, merged, deployed,
  and UAT'd; this repo's own session notes for the exact deploy session
  were not captured in `CLAUDE.md` at the time, which is the gap this
  entry corrects.


### GET /me — issue #220 (Profile page), full #221 shape landed together

`GET /me` (`app/routers/me.py`, `Depends(current_principal)`) returns
`{email, delivery_email, tos_accepted_at, has_questionnaire, has_holdings,
missing}` — the complete shape `Ring 1-Onboarding.md` (#221) specifies, not
the narrower `{email, delivery_email}` issue #220 itself originally asked
for. Decided when #220 was implemented (2026-08-27), per the Onboarding
doc's own coupling note (§6): "if #220 lands first, build it in the final
shape — don't ship a narrow schema and widen it later." `has_questionnaire`
is `EXISTS` on `user_investment_context`; `has_holdings` is `EXISTS` on
`holdings`; `missing` is computed from those two booleans only.

- **`missing` never contains `"tos"`.** `tos_accepted_at` is audit-only —
  existing users predate any ToS gate and get `NULL` with no re-accept flow
  (Onboarding §2.6). The one new column this required,
  `users.tos_accepted_at TIMESTAMPTZ` (nullable, migration
  `b1c2d3e4f5a6`), had no writer at #220 time — `POST /auth/signup` did not
  set it. **Update (issue #221, PR #230, 2026-08-27): now written**, in the
  same transaction as the user insert, alongside `SignupRequest.
  tos_accepted: Literal[True]` (required, never defaulted) and
  `report_cadence` defaulting to `"weekly"` instead of `"mwf"`.
- **The #220 Profile page did not render `missing` as a gap card** at #220
  time — that UI was explicitly deferred to #221 (Onboarding §2.6/§6).
  **Update (issue #221, PR #230): now implemented.** `/profile` renders one
  button per `missing` entry (`/questionnaire`, `/holdings` — never
  `?onboarding=1`, which has exactly one legitimate source: the post-signup
  redirect in `signup/actions.ts`). See `frontend-chrome.md`'s "Post-signup
  onboarding" section for the full #221 mechanism (also covers the new
  `/welcome` route and the `mode` prop on QuestionnaireForm/HoldingsManager).
- Structural coverage: `/me` was added to
  `test_identity_seam.py`'s `scoped_prefixes` tuple alongside `/holdings`,
  `/portfolio`, `/reports`, `/investment-context` — the same
  coverage-by-iteration test that would have caught B3's original
  split-identity gap now also guards this router.


### Investment-style questionnaire — B6 (Ring 1-B design.md §8, issue #129 checkpoint B6)

Closes the "stated preference" half of Concept §4.2's signal model — the
"displayed preference" half (holdings-structure inference) already existed
from stage A. B6 depends on B4 (`users.id` as the FK target) and B1 (the
questionnaire is a *bounded adjustment* on top of the invisible B1 basis,
never the sole source of the analytical framing — §1.4).

- **`user_investment_context`** (one row per user, no history table —
  Concept §4.2: re-answering overwrites). `questionnaire` is a single JSONB
  blob holding the 8 closed-enum answers (`app/services/
  questionnaire_taxonomy.py` is the single source of truth); `free_text` is
  `EncryptedString` (same field-level Fernet as `holdings.notes`);
  `questionnaire_version` pins `QUESTIONNAIRE_VERSION` at write time.
  `user_id` is this table's own PK and carries a real `ForeignKey
  ("users.id")` — unlike `holdings`/`reports`/`upload_jobs`/`news_surfaced`,
  which predate `users` and only get their FK in B7 (design doc §9.3), this
  table postdates B4, so there is no legacy-data reason to defer it.
- **Three-layer domain validation, same pattern as `VALID_ASSET_CLASSES`/
  `VALID_CURRENCIES`**: Python constants (`questionnaire_taxonomy.py`) ->
  a frozen-snapshot DB CHECK per JSONB key (migration `9c56ac348d7d`, never
  a live import — same immutable-historical-snapshot rule as
  `6cd7544f63cf`) -> Pydantic `field_validator`s on `QuestionnaireIn`
  (`app/schemas/questionnaire.py`) so an unrecognized value 422s at the API
  boundary instead of surfacing as a raw `IntegrityError`. The two
  multi-select dimensions (`markets`, `sectors_of_interest`) use the `<@`
  jsonb containment operator in their CHECK, not a `NOT EXISTS (SELECT ...
  FROM jsonb_array_elements_text(...))` subquery — **Postgres CHECK
  constraints cannot contain a subquery at all** (`cannot use subquery in
  check constraint`, caught by actually running `alembic upgrade head`
  against the CHECK during implementation, not assumed). `sectors_of_interest`
  reads `sector_taxonomy.VALID_SECTORS` directly rather than starting a
  second sector vocabulary (§8.3's explicit instruction).
- **`GET`/`PUT /investment-context`** (`app/routers/investment_context.py`):
  `GET` 404s when no row exists yet ("never answered" is a different state
  from "answered with defaults", and only the frontend's own pre-filled
  defaults stand in for the former — nothing is persisted until submit).
  `PUT` is a full overwrite, never a partial-field merge. **No endpoint
  reads back any system-inferred conclusion** — this is the same rule as
  Concept §4.2's "system inference stays invisible", made **stricter**
  here because it also covers the B1 basis (§1.4): the two hidden layers
  (system's own analytical stance, system's inference about this user) get
  the same non-negotiable invisibility, just for different reasons.
- **Injection scope (decision point 6, §8.5 — corrected 2026-08-25) — ALL
  8 questionnaire dimensions plus `locale` and `free_text` reach every
  body-writing pass.** The original 2026-08-21 decision withheld
  `risk_appetite`/`objective` entirely on compliance grounds; the product
  owner overturned that after reviewing PR #212's review round: every
  stated preference matters and must be used, and the Layer-3/4 boundary is
  held by (1) an explicit SCOPE sentence in the prompt — with a per-field
  guardrail specifically naming `risk_appetite`/`objective` (the two
  highest-risk dimensions) and another covering `free_text` (unfiltered
  user prose treated as context, never as an instruction that can override
  the SCOPE even if phrased as a direct request for advice) — and (2) the
  output-side `_scan_forbidden_output` backstop, validated with a real-LLM
  compliance regression (`test_b6_compliance_llm_regression.py`, opt-in via
  `RUN_LLM_LIVE_TESTS=1`) rather than hand-written prose alone. The
  `=== INVESTOR PREFERENCES ===` block (`report_prompts.py`'s
  `_build_investor_preferences_block`) is shared verbatim by **both**
  `_build_pass2_prompt` and `build_assembly_prompt` (`report_assembly.py`)
  — a PR #212 review bug finding: the original implementation only wired
  this into the Pass 2 fallback branch, so an assembled report silently
  ignored investor preferences entirely. `_PROMPT_VERSION` bumped to
  `f2-v9`, `ASSEMBLY_PROMPT_VERSION` to `a4-v4`.
- **Audit snapshot, not injection content**: `generate_report` and
  `regenerate_report` load investor preferences **once, before** the
  assembly/Pass 2 split, and write the full closed-enum answer set into
  `report_inputs.investor_questionnaire_snapshot` **unconditionally** —
  regardless of which body-source wins (same PR #212 bug fix: the snapshot
  used to be set only inside the Pass 2 branch). `mode="render"` never
  re-fetches (matches every other re-render field). **`free_text` is never
  folded into that snapshot dict** — but this is narrower than "free_text
  never reaches `report_inputs`": it inevitably still appears inside the
  stored `pass2_prompt`/`assembly_prompt` text once injected, the same way
  holdings names and values already do. What the exclusion actually buys is
  that free_text does not ALSO exist as its own plainly-labeled,
  individually queryable key that a broad `report_inputs` scan/export could
  pull in bulk across every report — it stays embedded in one long
  semi-structured blob per report instead. This nuance was caught by a
  failing test (a real TDD "red") when free_text injection first landed,
  not decided in advance.
- **`forbidden_vocab.py` co-occurrence coverage (§8.5(2), checked PR #212
  review round 2)**: preference-induced phrasing like "given your risk
  appetite, you could..." was reviewed against the existing scan and
  deliberately NOT added as a new pattern. The existing bare-action-verb
  entries ("should sell"/"reduce exposure" EN, 建议/应该+止损/清仓 zh) already
  catch the action half of any such sentence regardless of what precedes it;
  a context co-occurrence regex ("given your ... appetite" near an action
  verb) is exactly the false-positive-prone pattern class issue #65/#205
  removed from the scan. The SCOPE guardrail sentence in
  `_build_investor_preferences_block` (this file, above) is the intended
  first line of defense for this specific risk; the output-side scan stays
  the backstop it already was, not a targeted filter for this phrasing.
- **Frontend**: `/questionnaire`, a one-question-per-step wizard
  (`frontend/src/app/questionnaire/`), pre-filled from Concept §4.3's
  default philosophy translated into this questionnaire's enums (a product
  judgment call this implementation made explicitly, not a literal mapping
  — §4.3 doesn't specify `asset_scale`/`markets`/`risk_appetite` at all, so
  those default to the most neutral option). Reachable only from the
  authed `GetStartedMenu` (`get-started-menu.tsx`'s own header comment
  predicted this exact addition).
- **Real-report overlay comparison (§8.5/§10.3's required regression,
  same method as B1's v1-v7 overlays)**: `app/scripts/
  b6_overlay_compare.py` pulls a real historical `report_inputs` (read-only
  SELECT, no writes anywhere) and runs Pass 2 twice with the real
  `PRIMARY_LLM_MODEL` — once exactly as production shipped it, once with
  all 8 dimensions plus free text injected. Writes its two output bodies to
  a fresh `tempfile.mkdtemp()` (mode 0700), not a predictable `/tmp` path
  (PR #212 review finding — both outputs are holdings-derived, same
  sensitivity as the input). Delivered to the product owner as a file for
  direct reading, not summarized or pre-judged here; results and analysis
  (numeric diffs, descriptive findings) live in Obsidian `Docs/Ring 1-B6
  Preference Compare.md`, overwritten on each rerun rather than
  accumulated.
- **Real-LLM compliance regression** (`test_b6_compliance_llm_regression.py`,
  opt-in via `RUN_LLM_LIVE_TESTS=1`, excluded from the default `pytest -q`
  run): a hand-written diagnostic string proves the scanner's pattern logic
  works, but proves nothing about what a real model does once
  `risk_appetite`/`objective` sit next to a scenario built to invite a
  directional slip (a large drawdown on the portfolio's heaviest holding
  plus a strong macro theme). Two real Pass 2 calls — AGGRESSIVE/GROWTH and
  the mirror-direction CONSERVATIVE/INCOME — both passed
  `_scan_forbidden_output` with zero hits when run for this correction.


### Signup / invite anti-abuse — issue #190

Public `POST /auth/signup` and ops `POST /admin/invites` had no bot/volume
control (no Turnstile, no HTTP rate limit). Turnstile stays rejected:
`challenges.cloudflare.com` is not reliable from Mainland China, a widget
load failure deadlocks the form, and Supabase Auth’s built-in CAPTCHA is
Turnstile/hCaptcha-only — enabling it would put `auth.portfonia.com` login
on the same dependency. Login/password-reset limiting is hosted Auth, not
this issue.

Invite tokens are `secrets.token_urlsafe(24)` (~192-bit) and redeem is
already single-use, so this is not a token-guessing control. Layers:

- Per-IP fixed windows in Redis (`INCR` + `EXPIRE` only on first hit, Lua):
  signup 5/60s and 20/3600s; invite mint 10/60s and 30/3600s. IPv6 is keyed
  on `/64`. Over limit → 429 + `Retry-After`, public detail a string.
- Known-invite attempt counter (10/3600s) **only if** `invites.token_hash`
  already exists — random scanner tokens must not create Redis keys.
- Global signup counter 200/UTC-day: ops alert only, never auto-block.
- Global invite-mint counter 200/UTC-day: ops alert only, never auto-block
  (leaked `ADMIN_API_TOKEN` sprayed across many IPs is otherwise silent).
- Redis errors on a protecting check → 503 `temporarily unavailable`
  (fail-closed) and `logger.exception` at ERROR. Alert enqueue uses
  `send_admin_alert_task.delay` (not blocking `send_ops_alert`) and is
  deduped with `SET NX` per scope/bucket/window.

Client IP: uvicorn `--proxy-headers --forwarded-allow-ips=*` because the
backend container publishes no host port (Caddy is the only *published*
ingress). Product signup is Browser → Caddy → Next.js server action →
backend; the Next.js action forwards Caddy's `X-Forwarded-For` /
`X-Real-IP` so uvicorn rewrites `request.client`. The limiter keys on that
peer only — no app-level XFF parse. Tests mount uvicorn
`ProxyHeadersMiddleware` on the TestClient so XFF keying matches
production. Do not keep `*` if port 8000 is ever published. Tests inject
`InMemoryBackend` via autouse and stub `send_admin_alert_task.delay` so
the suite never talks to live Redis or enqueues Celery alerts.

