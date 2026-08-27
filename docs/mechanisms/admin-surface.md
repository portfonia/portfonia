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
- **User hard-purge** (issue #199): `DELETE /admin/users/{user_id}?confirm={email}` hard-deletes one user's own rows (`news_surfaced`, `reports`, `holdings`, `upload_jobs`, `user_investment_context`, then `users`) and clears invite pointers (`invites.used_by_user_id` nulled without touching `used_at`/`revoked_at`; other users' `invited_by` nulled). Refuses the seed `DEV_USER_ID`, any user who still has `invites.created_by` rows, a missing `confirm` query param, and a `confirm` that does not match the row's normalized email (strip + lowercase, same as signup). Does not delete the Supabase Auth account, does not touch global capture tables, and does not soft-delete via `users.status`. Spec: issue #199 comment dated 2026-08-27.
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

