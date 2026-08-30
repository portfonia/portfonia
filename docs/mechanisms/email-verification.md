# Generic email verification

## Core mechanism + Ops API — issue #260, PR #261

Full design: Obsidian `Hermes/Portfonia/Docs/Ring 1-Email Validation.md`.
This entry records the implementation as it actually landed, including
findings from an independent review round (blacktomb42, PR #261) that
changed the shape of several pieces from the design doc's first draft.

**What this issue ships**: a reusable `email_verifications` table, a
click-to-confirm flow (GET-inert status lookup + self-hosted Altcha PoW +
POST confirm), an async delivery-status poll against Resend, and an Ops API
surface (`POST`/`GET /admin/email-verifications`) to drive and inspect it.
**What it deliberately does not ship**: any application-scenario caller —
no signup hook, no Profile page integration, no report-generation gating,
no email-embedded unsubscribe. Until one of those lands, the Ops API is the
only way to create a verification record at all.

### Data model

`email_verifications` (`app/models/email_verification.py`): `user_id`
(nullable, `FK users.id ON DELETE RESTRICT`), `purpose`
(`account_email`/`delivery_email`/`ops_manual` — a closed, frozen-snapshot
`CheckConstraint`, same discipline as `6cd7544f63cf`/`e1f2a3b4c5d6`;
`revoked` and the Vigil-reuse placeholder purposes from the design doc are
NOT in this migration — nothing creates them yet), `email`, `token_hash`
(sha256, hash-only — same discipline as `invites.token_hash`), `status`
(`pending`/`verified`/`expired`/`superseded`/`undeliverable`),
`expires_at`, `verified_at`, `last_sent_at`, `resend_count`,
`provider_message_id`. A `user: Mapped[User | None] = relationship(lazy=
"raise", passive_deletes=True)` is declared purely for unit-of-work flush
ordering — same empirically-required pattern as `Holding.user`/
`Account.user`; a bare `ForeignKey()` column does not give SQLAlchemy this
ordering on its own, and its absence here originally broke this table's own
test fixtures that add a `User` and a bound `EmailVerification` in one
flush.

`users` gains two denormalized timestamps: `email_verified_at`,
`delivery_email_verified_at`. These are the intended hot-path read for a
future report-send gating consumer (design doc §3.6) — not built yet, so
nothing reads them today except this mechanism's own confirm/creation code.

### Confirm flow

1. `create_verification(session, email, purpose, user_id=None)`
   (`app/services/email_verification.py`) normalizes the email
   (`app.services.invites._normalize_email` — the same helper signup uses,
   not a second implementation), checks the resend cooldown, then **sends
   the email first** — the DB is only touched (supersede the prior live
   candidate, insert the new row, stamp `last_sent_at`, commit) once the
   send has confirmed success; only then is the delivery poll scheduled
   (below). Send-then-persist, not persist-then-send: an earlier version
   of this function committed first (round 1), on the reasoning that a
   Resend hiccup shouldn't lose the record of intent — that reasoning does
   not hold here, because unlike a plain insert, this step also supersedes
   an existing live record, so a failed send after persisting would have
   destroyed a still-working prior link while never actually sending
   anything (round 2 fix, review PR #261). A failed send raises
   `VerificationSendFailed` with zero DB writes — safe to retry.
2. Supersede scope is `(user_id, purpose)` when `user_id` is bound, but
   `(purpose, email)` when it's an unbound `ops_manual` probe
   (`user_id=None`) — `purpose=ops_manual` always carries `user_id=None`
   (§3.5: a bound Ops call passes `account_email`/`delivery_email`
   instead), so scoping by `(user_id, purpose)` alone would group every
   unbound probe together regardless of address, retiring an unrelated
   still-pending probe.
3. A 60-second per-scope resend cooldown (`RESEND_COOLDOWN`) sits in front
   of the supersede step — `create_verification` raises `ResendTooSoon`
   (mapped to `429` by the Ops router) rather than resending on every call
   of a tight loop. This is deliberately data-driven (a query against the
   existing pending row's `last_sent_at`), not the Redis multi-bucket
   limiter `rate_limit.py` uses elsewhere — the only caller today is the
   `ADMIN_API_TOKEN`-gated Ops API, so there's no untrusted-facing abuse
   surface yet that would need that heavier machinery.
4. `GET /email-verifications/status?token=` (`app/routers/
   email_verification.py`) is completely inert — no writes, including no
   persisted "expired" transition for a row whose `expires_at` has passed
   (that transition only happens inside the POST). This mirrors Vigil
   Concept & Design §4.2's reasoning: email security gateways (Outlook Safe
   Links, Gmail link scanning) prefetch links in transit, and a GET that
   mutates state would let a prefetch look like a confirmation.
5. `POST /email-verifications/confirm` (token + Altcha payload) is the only
   state-changing step. **`purpose=account_email` never overwrites
   `users.email`** — it only marks `email_verified_at` when the record's
   candidate address still matches the account's current email, and
   rejects (the same generic `VerificationRejected` message every other
   failure here uses) on a mismatch. `users.email` is unique and is
   Supabase Auth's login identity; an earlier draft of this PR unconditionally
   assigned it, which would either desync local email from Auth's sign-in
   address, or 500 via a unique-constraint `IntegrityError` if the address
   already belonged to someone else (round-1 review finding). `purpose=
   delivery_email` keeps the original design: writes the new address into
   `users.delivery_email` and sets `delivery_email_verified_at`.

   **Both status transitions here (`pending`→`expired`, `pending`→
   `verified`) are conditional `UPDATE ... WHERE status='pending'` writes
   with a `rowcount` check, not plain attribute assignment on the row
   loaded earlier (round-3 review finding).** An earlier version did
   `record.status = "verified"` directly, which flushes as an unconditional
   `UPDATE ... WHERE id=:id` — no status guard. A confirm click that reads
   `pending` and then races a concurrent supersede (an Ops resend that just
   passed the cooldown) or a concurrent expiry could still commit
   `verified` after the row had already moved on, resurrecting a dead
   token and, for `delivery_email`, writing that now-superseded record's
   (possibly stale) email into `users.delivery_email` after a newer resend
   was supposed to have replaced it — the same race class
   `poll_email_verification_delivery` was fixed to avoid in round 1, just
   never applied to this function. The `users` write-back now only happens
   after the conditional UPDATE's `rowcount` confirms this call actually
   won the `pending`→`verified` transition.

### Delivery-status poll (design doc §3.3 step 6)

`app.tasks.email_verification_tasks.poll_email_verification_delivery`,
scheduled ~10 minutes after a successful send (`apply_async(countdown=
POLL_DELAY_SECONDS)`), polls Resend's `GET /emails/{id}` for the send's
`last_event`. On `bounced`/`complained`/`failed`/`suppressed`, it marks the
row `undeliverable` via a **conditional `UPDATE ... WHERE status =
'pending'`**, not a plain attribute assignment on the row it loaded at the
start of the task — the task runs on its own `SessionLocal()`, a separate
connection from whatever session a concurrent confirm click commits
through, and under READ COMMITTED the initial "is this still pending" read
can be stale by the time the poll fires 10 minutes later. The `WHERE`
clause makes the write row-level-conditional so a bounce detected after a
successful click can never overwrite a `verified` (or `expired`/
`superseded`) row back to `undeliverable` (round-1 review finding).

**Two separate Resend API keys, not one.** `RESEND_API_KEY` (existing,
`sending_access`-scoped) cannot call `GET /emails/{id}` — confirmed against
Resend's own docs, and directly against Resend's live API before wiring
this in (a bogus email id returns `404 "Email not found"` with a
`full_access` key, not a permission error). `RESEND_ALL_ACCESS_API_KEY`
(`app/core/config.py`, optional — the poll task no-ops silently when unset)
is a **separate** key, deliberately not an upgrade of the existing one: the
send path has no reason to hold a key that can read/write arbitrary Resend
resources.

**Registering the task with the worker is not automatic.** The task module
existed and `create_verification` enqueued it correctly, but
`app/tasks/__init__.py`'s `Celery(... include=[...])` list — the only thing
an actual worker process consults at startup to know which task modules to
import — did not name it (round-1 review finding). The API process could
still enqueue the message fine (it imports the module directly), which is
exactly why this was invisible without an explicit check: `test_poll_task_
module_is_in_the_celery_app_include_list` asserts `celery_app.conf.include`
directly rather than `celery_app.tasks`, because asserting the latter would
have been satisfied by the test file's own direct import of the task
module regardless of what `include` said.

### Purge (hard-delete) interaction

`email_verifications.user_id`'s `ON DELETE RESTRICT` FK means `DELETE
/admin/users/{id}` (issue #199/#225/B7) must delete a user's bound
verification rows before deleting the `users` row itself, the same class
of requirement B7 already established for `holdings`/`reports`/
`upload_jobs`/`news_surfaced`/`accounts`. `app/services/user_purge.py`
deletes `email_verifications` (scoped to `user_id`, so unbound `ops_manual`
probes with `user_id=NULL` are never touched by any user's purge) right
after `user_investment_context`; `PurgeResult`/`PurgeDeletedCounts` and the
Ops response gained the matching `email_verifications` count field
(round-1 review finding — this table's FK existed from the start of this
PR, but the purge path was never updated for it).

### Ops API

See Obsidian `Hermes/Portfonia/Docs/Ops API Reference.md` for the endpoint
reference (auth, request/response shapes, curl examples, error table). One
shape note not obvious from the design doc: **`POST`'s response is
deliberately narrower than `GET`'s** — `POST` returns only
`{id, status, expires_at}` (never the plaintext token), while `GET` is
widened to include `email`, `purpose`, `user_id`, `provider_message_id`,
`last_sent_at`, `verified_at` — the whole point of the diagnostic `GET` is
answering "why didn't this user get their email" without a database query
(round-1 review finding; the original `GET` shape matched `POST`'s narrow
one, which made it useless for that stated purpose). `CreateEmailVerificationBody`
also rejects (`422`) any `purpose`/`user_id` pairing other than
`ops_manual`+no-`user_id` or `account_email`/`delivery_email`+a real
`user_id` — the other combinations previously persisted a pending row that
could never do anything useful on confirm (round-1 review finding).

### Frontend

`/verify-email` (Next.js, `frontend/src/app/verify-email/`) mirrors
`/forgot-password`'s structure exactly: a Server Component does the inert
`GET` status lookup, a client form reuses the same self-hosted Altcha
widget pattern (`AltchaWidget` in this route's own `_components/`, not a
shared extraction — matches this codebase's existing one-widget-per-route
convention), and a Server Action does the one state-changing `POST`
confirm call. No `next.config.ts` change was needed — the existing
catch-all `/api/:path*` rewrite already covers the new backend endpoints.
`/verify-email` is listed in `proxy.ts`'s `PUBLIC_PATH_PREFIXES` (same
reasoning as `/forgot-password`/`/reset-password`: the token itself is the
credential, no session required). New `emailVerification` locale namespace
in `frontend/src/locales/{en,zh-Hans,zh-Hant}.json`.
