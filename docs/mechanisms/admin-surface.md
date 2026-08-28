# Admin surface implementation detail

## Admin surface: API endpoint first, UI later (MANDATORY)

Any feature with an **administrative purpose** — something only the product
owner uses, not part of a normal user's journey — ships first as an
`/admin/*` API endpoint authenticated by an ops token. A management UI is an
optional layer on top of those endpoints, never a prerequisite for the
capability existing.

- **Status**: implemented (issue #129 Ring 1 stage B, checkpoint B2,
  2026-08-22). `app/routers/admin.py` (`APIRouter(dependencies=[Depends(
  require_ops_token)])`) mounts at `/admin` in `main.py`; `require_ops_token`
  lives in `app/core/deps.py`. `POST /admin/portfolio/refresh` was the first
  real endpoint (moved from the now-removed `POST /portfolio/refresh` —
  decision point 8/11: a global market-data refresh is an ops action, not
  something an individual user should trigger). B4 added
  `POST/GET/DELETE /admin/invites` and `POST /admin/users/{id}/bind-subject`
  under the same ops-token router (plaintext invite token returned once on
  create; bind-subject never overwrites a non-NULL `auth_subject`;
  issue #188/PR #219: an email-bound `POST /admin/invites` → **409** when
  `users.email` already holds the normalized address — no status filter,
  same predicate as `POST /auth/signup` via the shared `signup_email_taken`
  helper in `app/services/invites.py`; generic invites and the redeem-side
  undistinguishable `InviteRejected` are unchanged). Issue
  #201 (PR #203) added `POST /admin/users/{id}/reports/generate`: ops-token,
  synchronous `generate_report` for one user (`session_node="manual"`),
  hitting `api.portfonia.com` directly so the Next.js proxy timeout on
  self-service `POST /reports/generate` (issue #193) is not in the path.
  404 if the user is missing; 422 if not active or has no holdings (mirrors
  `active_user_ids()`); `openai.APIError` → 502; concurrent unique-key race
  → 409. Success emails the target user. Admin email-resend was scoped out
  (`POST /admin/reports/{id}/send` leftover). A structural test
  (`test_all_admin_routes_require_ops_token` in `test_admin_router.py`)
  iterates `app.routes` and asserts every `/admin`-prefixed route's
  dependant chain includes `require_ops_token`, so a future endpoint that
  forgets to opt in fails CI rather than shipping unauthenticated.
- **User hard-purge** (issue #199, extended by issue #225): `DELETE /admin/users/{user_id}?confirm={email}` hard-deletes one user's own rows (`news_surfaced`, `reports`, `holdings`, `upload_jobs`, `user_investment_context`, then `users`) and clears invite pointers (`invites.used_by_user_id` nulled without touching `used_at`/`revoked_at`; other users' `invited_by` nulled). Refuses the seed `DEV_USER_ID`, any user who still has `invites.created_by` rows, a missing `confirm` query param, and a `confirm` that does not match the row's normalized email (strip + lowercase, same as signup). Does not touch global capture tables, and does not soft-delete via `users.status`. Spec: issue #199 comment dated 2026-08-27.
  - **Supabase Auth is now purged in the same call (issue #225), sequenced strictly before any local delete**: if the local row has `auth_subject` set, `delete_auth_user(sub)` runs first, outside any local transaction. A 404 (already gone) is idempotent success. Any other `AuthProviderError` aborts the request with `502` before `purge_user()` or `session.commit()` ever run — nothing local is touched, so the caller can always retry safely. This ordering exists specifically because Postgres and Supabase Auth have no shared transaction: whichever side deletes first is the one that must be safe to redo. Response gains `auth_deleted: bool` — `true` only when an Auth user was actually found and removed; `false` both when the row had no `auth_subject` and when it did but Supabase had nothing to delete (a prior partial cleanup).
  - **Orphan-only path (issue #225 requirement B)**: when `session.get(User, user_id)` is `None`, the endpoint no longer 404s immediately. **First it checks whether `user_id` is actually a live user's `auth_subject`** (round 2 review — a real bug in the initial cut: a PK miss on `users.id` is not proof there's no local user, since `user_id` could be someone's Auth `sub` passed by mistake; without this check the endpoint would Auth-delete a live account, including the seed user's, while its local row sits untouched — the exact reverse of the orphan #225 exists to clean up). A hit there is a `409` before any Auth call is ever made, pointing the caller at the correct `users.id`. Only past that guard does it call the new `get_auth_user(str(user_id))` (Supabase Auth ids are UUIDs, same shape as the path param). If a matching Auth user exists, this is the exact gap that motivated #225 (a local row cleaned up before this endpoint could sequence Auth deletion, leaving a live orphan account): the same `confirm` contract applies but compares against the Supabase user's email, the seed-user and `created_invites` guards are skipped (both are local-row-scoped), `delete_auth_user` runs, and the response reports all local table counts as `0` with `auth_deleted=true`. Only when *no* local PK, *no* local `auth_subject`, and *no* Supabase account match does the endpoint still 404. A GoTrue fault on the lookup (5xx, timeout, malformed body) also maps to `502`, same as the delete half — never an unhandled 500.
  - **`{user_id}` means a different column depending on which branch runs — operators must know which id they're holding** (B4: `users.id` is our own PK, never the same value as `auth_subject`). When a local row exists, `{user_id}` is `users.id` and the Auth account is found via that row's `auth_subject`. When it doesn't, `{user_id}` in the URL must be the *Supabase Auth UUID* — a `users.id` from before the local row was deleted (e.g. copied out of an old UAT note) will 404 even if the Auth orphan is still there, because nothing links that stale id back to Auth once the local row is gone. Get the Auth UUID from the Supabase Dashboard, the earlier purge response's `auth_deleted`/audit log, or a prior `get_auth_user` lookup.
  - **`app/services/auth_provider.py` gained one function and one changed return type**: `get_auth_user(sub) -> AuthUserInfo | None` (`GET {issuer}/admin/users/{sub}`, `404`→`None`); `delete_auth_user(sub)` now returns `bool` (`True` = found and deleted, `False` = 404/already-gone) instead of `None` — existing callers (signup's compensation path) ignore the return value, so this is additive, not breaking.
  - **Signup's compensation failure is no longer silent** (issue #225 bug 2): if `delete_auth_user` itself raises inside `POST /auth/signup`'s compensation branch, `send_ops_alert` fires (previously: log line only) — a failed compensation now produces an actionable signal instead of a trace only discoverable by reading logs after the fact.
  - **Signup failure logging is now differentiated** (issue #225 bug 1): the `except Exception` branch in `POST /auth/signup` tags its log record with `signup_failure_reason` (`invite_rejected` / `auth_provider_error` / `integrity_error`) so ops monitoring can alert on auth-provider/DB faults without being drowned out by expected invite-rejection noise. The client-facing message is unchanged for all three — this is a server-side log field only, never surfaced in the response.
- **Auth**: `ADMIN_API_TOKEN` (`Settings`, `SecretStr`, required — no unset
  state, same discipline as `HOLDINGS_ENCRYPTION_KEY`) + optional
  `ADMIN_API_TOKEN_PREV` for a no-downtime rotation window (identical
  double-key pattern to `HOLDINGS_ENCRYPTION_KEY`/`_PREV`,
  `app/core/encryption.py`). Compared via `secrets.compare_digest`, never
  `==` (locked by a test spying on the real function). Header shape:
  `Authorization: Bearer <token>`. Missing/malformed/wrong token → `401`
  (not FastAPI's default 422 for a missing required header — B2's
  acceptance criteria treat "no token" as an auth failure).
- **The ops channel is deliberately NOT the user auth system.** It
  authenticates with a static bearer secret from `.env`, queries no tables,
  and does not depend on the user system existing. The reason is the failure
  mode it has to survive: the channel must still work when the login system
  itself is broken and is the thing being repaired. Hanging it off the same
  auth welds the only repair path to the fault source. (B2 shipped before
  the `users` table existed; B4 added `users`, and ops-token auth still does
  not read it.)
- **Every `/admin/*` call is audit-logged** (`AdminLoggingRoute` in
  `app/routers/admin.py`, the router's `route_class`) — endpoint, method,
  path/query params, status code, duration, via the existing
  `log_ops_event` (`app/core/ops_log.py`). Never logs the Authorization
  header or token value. A run of 5 consecutive 401s on `/admin/*` fires
  one alert, then resets — a sustained guessing attempt doesn't resend the
  alert on every subsequent request; any 200 resets the counter. **The
  alert is enqueued via `send_admin_alert_task.delay()`
  (`app/tasks/admin_tasks.py`), never called as `send_ops_alert(...)`
  directly from the router** — that function makes a blocking
  `httpx.Client(timeout=15.0)` call, and this is an async request path on
  what may be a single-uvicorn-worker box; a direct call would stall the
  event loop for up to 15s on every 5th unauthorized hit (PR #177 review
  round 3). Route new work needing "do this without blocking the request"
  through the existing Celery queue (every other `send_ops_alert` call
  site in this repo already does) — do not reach for a process-local
  workaround (Starlette `BackgroundTask`, `asyncio.create_task`, etc.)
  before checking whether the queue already covers it.
- **Living endpoint reference**: every implemented and planned `/admin/*`
  endpoint (path, auth, params, curl example) is tracked in Obsidian
  `Hermes/Portfonia/Docs/Ops API Reference.md` — update it in the same
  change that adds/modifies/removes an endpoint, not at stage cleanup.
- Consequence to accept openly: some capabilities will exist with **no user
  interface**, reachable only via curl or an agent calling the endpoint. That
  is the intended tradeoff, not an oversight.

Full design, including token rotation, constant-time comparison, router-level
auth declaration, and audit logging: Obsidian `Hermes/Portfonia/Docs/Ring 1-B design.md` §4.

