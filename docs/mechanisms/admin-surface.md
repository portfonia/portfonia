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
  404 if the user is missing; 422 if not active (the original no-holdings
  422 was removed by issue #221; `active_user_ids()` itself gained a
  per-cadence holdings gate in issue #191, still required for `mwf`, not
  `weekly` — see the "Cadence change" bullet below); `openai.APIError` →
  502; concurrent unique-key race
  → 409. Success emails the target user. Admin email-resend was scoped out
  (`POST /admin/reports/{id}/send` leftover). A structural test
  (`test_all_admin_routes_require_ops_token` in `test_admin_router.py`)
  iterates `app.routes` and asserts every `/admin`-prefixed route's
  dependant chain includes `require_ops_token`, so a future endpoint that
  forgets to opt in fails CI rather than shipping unauthenticated.
- **Cadence change** (issue #191): `POST /admin/users/{user_id}/cadence`
  sets `users.report_cadence`, same body/response/404 shape as
  `bind-subject`. `report_cadence: Literal["mwf", "weekly"]` on the request
  body gives a clean 422 on a bad value instead of an `IntegrityError`
  bubbling up from the DB `CheckConstraint` — kept in sync with
  `VALID_REPORT_CADENCES` (`app/models/user.py`) by hand, since a Pydantic
  `Literal`'s members have to be compile-time. Intended to be reusable later
  for self-service cadence selection (post-auth, post-billing) — that reuse
  is explicitly out of scope for the PR that added this endpoint. See the
  "Cadence" bullet in `docs/mechanisms/capture-and-reporting.md` for how
  `report_cadence` actually drives scheduling.
- **User hard-purge** (issue #199, extended by issue #225, checkpoint B7, and issue #260): `DELETE /admin/users/{user_id}?confirm={email}` hard-deletes one user's own rows (`news_surfaced`, `reports`, `holdings`, `accounts`, `upload_jobs`, `user_investment_context`, `email_verifications`, then `users`) and clears invite pointers (`invites.used_by_user_id` nulled without touching `used_at`/`revoked_at`; other users' `invited_by` nulled). Refuses the seed `DEV_USER_ID`, any user who still has `invites.created_by` rows, a missing `confirm` query param, and a `confirm` that does not match the row's normalized email (strip + lowercase, same as signup). Does not touch global capture tables, and does not soft-delete via `users.status`. Spec: issue #199 comment dated 2026-08-27.
  - **`accounts` deletion (issue #129 B7) runs after `holdings`, before `upload_jobs`/`user_investment_context`/`users`** — `holdings.account_id` FKs to `accounts.id` `ON DELETE RESTRICT`, so an account row can't be deleted while a holding still points at it; holdings are always gone by the time this step runs. Response's `deleted` object gains an `accounts` count.
  - **`email_verifications` deletion (issue #260) runs after `user_investment_context`, before the invite-pointer cleanup/`users`** — `email_verifications.user_id` FKs to `users.id` `ON DELETE RESTRICT`, same class of requirement as `accounts` above. Scoped to `user_id`, so an unbound `ops_manual` probe (`user_id` NULL) is never touched by any user's purge. Response's `deleted` object gains an `email_verifications` count (this was missing from the initial PR #261 implementation — the FK existed from the start but the purge path wasn't updated for it until an independent review caught it).
  - **Supabase Auth is now purged in the same call (issue #225), sequenced strictly before any local delete**: if the local row has `auth_subject` set, `delete_auth_user(sub)` runs first, outside any local transaction. A 404 (already gone) is idempotent success. Any other `AuthProviderError` aborts the request with `502` before `purge_user()` or `session.commit()` ever run — nothing local is touched, so the caller can always retry safely. This ordering exists specifically because Postgres and Supabase Auth have no shared transaction: whichever side deletes first is the one that must be safe to redo. Response gains `auth_deleted: bool` — `true` only when an Auth user was actually found and removed; `false` both when the row had no `auth_subject` and when it did but Supabase had nothing to delete (a prior partial cleanup).
  - **Orphan-only path (issue #225 requirement B)**: when `session.get(User, user_id)` is `None`, the endpoint no longer 404s immediately. **First it checks whether `user_id` is actually a live user's `auth_subject`** (round 2 review — a real bug in the initial cut: a PK miss on `users.id` is not proof there's no local user, since `user_id` could be someone's Auth `sub` passed by mistake; without this check the endpoint would Auth-delete a live account, including the seed user's, while its local row sits untouched — the exact reverse of the orphan #225 exists to clean up). A hit there is a `409` before any Auth call is ever made, pointing the caller at the correct `users.id`. Only past that guard does it call the new `get_auth_user(str(user_id))` (Supabase Auth ids are UUIDs, same shape as the path param). If a matching Auth user exists, this is the exact gap that motivated #225 (a local row cleaned up before this endpoint could sequence Auth deletion, leaving a live orphan account): the same `confirm` contract applies but compares against the Supabase user's email, the seed-user and `created_invites` guards are skipped (both are local-row-scoped), `delete_auth_user` runs, and the response reports all local table counts as `0` with `auth_deleted=true`. Only when *no* local PK, *no* local `auth_subject`, and *no* Supabase account match does the endpoint still 404. A GoTrue fault on the lookup (5xx, timeout, malformed body) also maps to `502`, same as the delete half — never an unhandled 500.
  - **`{user_id}` means a different column depending on which branch runs — operators must know which id they're holding** (B4: `users.id` is our own PK, never the same value as `auth_subject`). When a local row exists, `{user_id}` is `users.id` and the Auth account is found via that row's `auth_subject`. When it doesn't, `{user_id}` in the URL must be the *Supabase Auth UUID* — a `users.id` from before the local row was deleted (e.g. copied out of an old UAT note) will 404 even if the Auth orphan is still there, because nothing links that stale id back to Auth once the local row is gone. Get the Auth UUID from the Supabase Dashboard, the earlier purge response's `auth_deleted`/audit log, or a prior `get_auth_user` lookup.
  - **Email-first sibling route (issue #274)**: `DELETE /admin/users/by-email?email={email}&confirm={email}` is the additive sibling for callers who only have an email — it removes the SSH+psql "look up the user_id first" step entirely. Both query params are required and normalized (strip + lowercase, same `_normalize_email`); a mismatch after normalization is `422` (`email and confirm must match`) — a self-consistency repeat check, deliberately weaker than the by-id id/email cross-check (the email is the single fact the caller must get right). Local `users` lookup by normalized email runs the exact same guards and ordered purge as the by-id route via the shared `_purge_local_user` (seed-user/`created_invites` 409s, Auth-before-local, same 10-step delete). On a local miss it falls through to the Auth orphan path via `get_auth_user_by_email` (see below), with the same `502` mapping on lookup/delete failure and the same `404` only when neither side has the address. The response keeps the `PurgeUserOut` shape, with `user_id` reporting which row was actually resolved and deleted.
  - **`get_auth_user_by_email(email) -> AuthUserInfo | None`** (issue #274): the orphan-path lookup for the by-email route. GoTrue's `GET /admin/users` list endpoint does **not** filter by email — it ignores an `email` query param (returns the audience's unfiltered first page) and only narrows via the substring `filter` param (`email LIKE %filter%` or full_name ILIKE). The helper therefore passes the normalized address as `filter`, pages through every substring hit (`page`/`per_page`), and returns only a user whose stored email normalizes to exactly the query — never the first row of an unfiltered page. On the by-email orphan path, before `_auth_delete_or_502`, the router also occupancy-checks the resolved Auth id against live `users.auth_subject` — email drift can otherwise reverse-orphan: a live local row bound to that Auth account under a different local email would get its Auth account deleted while the local row stands (same class of guard the by-id path 409s on, PR #246 round 2).
  - **`app/services/auth_provider.py` gained one function and one changed return type**: `get_auth_user(sub) -> AuthUserInfo | None` (`GET {issuer}/admin/users/{sub}`, `404`→`None`); `delete_auth_user(sub)` now returns `bool` (`True` = found and deleted, `False` = 404/already-gone) instead of `None` — existing callers (signup's compensation path) ignore the return value, so this is additive, not breaking.
  - **Signup's compensation failure is no longer silent** (issue #225 bug 2): if `delete_auth_user` itself raises inside `POST /auth/signup`'s compensation branch, `send_ops_alert` fires (previously: log line only) — a failed compensation now produces an actionable signal instead of a trace only discoverable by reading logs after the fact.
  - **Signup failure logging is now differentiated** (issue #225 bug 1): the `except Exception` branch in `POST /auth/signup` tags its log record with `signup_failure_reason` (`invite_rejected` / `auth_provider_error` / `integrity_error`) so ops monitoring can alert on auth-provider/DB faults without being drowned out by expected invite-rejection noise. The client-facing message is unchanged for all three — this is a server-side log field only, never surfaced in the response.
- **Report rerun-and-resend — implemented (issue #324, PR #326)**: on
  2026-09-02 a user's holdings were re-uploaded to fix an
  `asset_class`/`market` misclassification, and the already-generated,
  already-emailed report for that day needed to reflect the fix without
  re-fetching news/Tavily/macro intel (that data is cached in
  `report_inputs`; re-fetching wastes budget and can introduce
  non-determinism). No `/admin/*` endpoint covered this at the time, so it
  was done once via SSH + a one-off `docker compose exec -T backend python <
  script` invocation against production, directly manipulating
  `reports`/`users` rows (full investigation notes: issue #324's first
  comment). Filed as a real gap per the "API endpoint first" rule above
  rather than left as a recurring SSH drill, and closed by this endpoint.
  - **`POST /admin/users/{user_id}/reports/{report_id}/rerun`**, same
    ops-token auth as every other route here (not the self-service `POST
    /reports/{id}/regenerate`, which is scoped to the caller's own
    principal and never clears `email_sent_at` — it cannot rerun another
    user's report or force a genuine resend). Body: `{"mode": "analyze" |
    "render", "resend": bool}`, both optional, defaulting to `"analyze"`
    and `true`. `mode` is a `Literal` on the request model, so an invalid
    value is a `422` from FastAPI's own request validation before the
    handler body runs. Response 200:
    `{report_id, user_id, status, mode, email_sent_at,
    provider_message_id}`.
  - **`mode="analyze"` (default)** re-runs the body pass against a
    **fresh** read of the user's live `Holding` rows via
    `regenerate_report(..., mode="analyze")` → `compute_portfolio` — this
    is what actually picks up a holdings/asset_class correction.
    `mode="render"` only re-renders the stored body (formatting/output-
    language iteration, never picks up a holdings change).
  - **`resend=true` (default)**: the handler nulls `email_sent_at` and
    `provider_message_id` on the target `Report` row and flushes **before**
    calling `regenerate_report` — never a physical `DELETE` (that would
    destroy the `report_inputs` JSONB cache that makes a no-refetch rerun
    possible in the first place). Clearing first, not after, matters
    because `send_report_email`'s G3 dedup guard silently no-ops on any
    report where `email_sent_at` is already set — without this ordering a
    rerun could produce a genuinely corrected body that still never goes
    out. Only if the resulting `report.status == "success"` does the
    handler explicitly call `send_report_email(report, session)` —
    `regenerate_report` itself never emails (same as the self-service
    route). `resend=false` leaves `email_sent_at` untouched and never
    sends, identical in effect to the self-service regenerate.
  - **Path params `user_id` + `report_id`, both required** (mirrors
    `regenerate_report`'s own `Report.id == report_id, Report.user_id ==
    user_id` ownership filter rather than trusting `report_id` alone): an
    unknown `user_id` and a `report_id` that exists but belongs to a
    different user both 404 with the same `"report not found"` detail —
    the route does not distinguish the two to a caller (no user-enumeration
    signal), consistent with the rest of this file's 404 conventions.
  - **Error mapping**: `401` missing/wrong ops token (router-level, as
    always) · `404` unknown `user_id` (`"user not found"`) or unknown/
    not-owned `report_id` (`"report not found"`) or `regenerate_report`'s
    own `ValueError` (e.g. no stored body to regenerate from) · `422`
    invalid `mode` · `502` `LLMEmptyResponseError` or `openai.APIError`
    during the body-pass rerun, `openai.APIError` message reused verbatim
    from the generate endpoint's own mapping.
  - **Output language**: `report_language_for(session, user_id,
    Settings.OUTPUT_LANG)` — the target user's own `users.locale` (issue
    #308), falling back to the system default only if the row can't be
    resolved. Same convention as the self-service regenerate and the
    generate-for-user admin endpoint.
  - **Deliberately out of scope (per the design contract, issue #324's
    second comment)**: a bulk "rerun every report for this user today"
    variant, and a by-`report_date` convenience lookup in place of
    requiring the caller to already know `report_id` — `GET /admin/users`
    already resolves `user_id` by email, but there is no read endpoint to
    list a user's `reports` rows by date; add one only if caller
    ergonomics turn out to need it.
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

