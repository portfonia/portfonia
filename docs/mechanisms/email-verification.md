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
future report-send gating consumer (design doc §3.6) — the gate itself is
not built yet, but `GET /me` already exposes both timestamps for the
Profile page's verification-state display (issue #269). `recipient_email()`
still ignores verification state entirely: reports are sent to unverified
addresses today (which is why the Profile page's no-recipient copy is
deliberately "make sure reports can reach you", never "reports will not be
sent" — see `frontend-chrome.md`'s Profile redesign entry).

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

   **Accepted deviation, not fixed:** the cooldown check
   (`_find_live_pending` + the `last_sent_at` comparison) and the supersede
   step are two separate statements, not one atomic operation. Two
   concurrent `create_verification` calls for the same scope can both read
   the same prior row as outside the cooldown, both send, and both insert a
   `pending` row — briefly two live pending records for one scope, which
   violates design doc §3.2's "one live pending record per scope"
   invariant (`_find_live_pending`'s `.limit(1)` with no `ORDER BY` would
   then pick between them arbitrarily on the next call). Disclosed and
   accepted by the product owner during PR #261's review (round 1 risk
   analysis, reconfirmed round 3): the only caller today is the
   `ADMIN_API_TOKEN`-gated Ops API, so the realistic trigger is an operator
   double-firing a curl command, not an adversarial race — a proper fix
   would need a DB-level uniqueness constraint or `SELECT ... FOR UPDATE`,
   deferred until a real caller surface (signup/Profile) makes the
   likelihood worth the complexity (round-4 review finding: this tradeoff
   was accurate in the PR thread but undocumented here, which is exactly
   the provenance this file exists to persist).
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

## Profile page integration + signup hook — issue #262, PR #263

Full design: Obsidian `Hermes/Portfonia/Docs/Ring 1-Profile Page.md` §八
(2026-08-30, including §8.7's product-owner decision to bundle the signup
hook into this issue). This entry records what actually landed, including
the independent review round (blacktomb42, PR #263, CHANGES_REQUESTED)
whose five findings were each verified against source before fixing.

**What this issue ships** (the first application-scenario callers of
`create_verification` — until now the Ops API was the only way to create a
verification record at all):

### §4.1 signup hook

New Celery task
`app.tasks.email_verification_tasks.send_account_email_verification_task`
wraps the unchanged `create_verification(email=user.email,
purpose="account_email", user_id=user.id)`. `signup` enqueues
`.delay(str(new_id))` **after** its compensation `try/except`: at that
point the account is fully created, so an enqueue failure must neither
fall into the `delete_auth_user` compensation path (which would destroy
the just-created Auth account) nor fail the signup response — it logs and
continues (best-effort). The task never runs inline: `create_verification`
makes a synchronous Resend HTTP call (15s timeout).

**Retry wiring was the review round's confirmed bug (blacktomb42, PR
#263)**: `max_retries=3` on the decorator retries nothing by itself —
without an explicit `self.retry()` call a transient Resend failure failed
the task once and the new user's verification email was silently lost,
despite the decorator implying otherwise. The task's docstring had
claimed exceptions "propagate for Celery's retry machinery", which is not
how Celery works (`autoretry_for` unset, no `self.retry()` call). Fixed
to the codebase's established shape (same as capture/backup/report/cache
tasks): on `VerificationSendFailed`, check
`self.request.retries >= self.max_retries` — at exhaustion log an ERROR
naming the only real recovery path (a fresh `POST
/admin/email-verifications`, since no Profile-page record exists to
resend from), otherwise `raise self.retry(exc=exc) from exc`. The
persist-failure path (email already sent) is deliberately NOT retried: a
blind retry would send a second email rather than recover the first, and
`create_verification` already logs that case loudly itself.

**A lost enqueue is not recoverable from the Profile page** (review
finding 2): no `email_verifications` row exists, so `GET /me`'s pending
list is empty and `POST /email-verifications/{id}/resend` has no id to
act on. Only the Ops API can create a fresh record. The signup-hook
comment originally claimed Profile-page recoverability and was corrected.

Two tests pin the wiring: one patches `self.retry` directly (asserting
`celery_app.tasks` would have been satisfied by the test file's own
import — same reason the `conf.include` regression test in PR #261
exists), one forces the exhaustion branch (`max_retries=0`) and asserts
the recovery-path log.

### `GET /me` extension (Profile Page.md §8.2)

New `pending_email_verifications: list[PendingVerificationOut]` (id,
purpose, email, status, expires_at, last_sent_at): the calling user's own
rows, `purpose IN (account_email, delivery_email)`, `status IN (pending,
undeliverable)`, ordered `last_sent_at` desc. `undeliverable` is included
deliberately (§8.2, from 2026-08-30 production testing): a typo'd address
that bounced leaves nothing visibly "pending", so listing only `pending`
would render "nothing waiting" for a user who needs to fix their
address. `expired`/`superseded`/`verified` are history, not actionable.
`ops_manual` rows are always `user_id=NULL` and can never match a user
scope; a dedicated test pins this anyway. No token hash or
provider_message_id leaves the backend.

### `POST /email-verifications/{id}/resend` (Profile Page.md §8.3)

Session-authenticated (`current_principal`, not `/admin/*`), wrapping the
**unchanged** `create_verification`. The ownership+status gate collapses
missing / foreign-owned / terminal-status / unbound-`ops_manual` rows
into one `404` (never `403` — no existence leak). Router-layer Redis
limiting via `rate_limit_enforce_resend_verification`: per-user 3/hour
plus a **global** per-address 3/hour bucket (sha256-keyed, deliberately
NOT scoped by user — several accounts aimed at one victim's address share
one allowance, Email Validation.md §3.4's mail-bomb scenario),
fail-closed `503` on Redis outage. `create_verification`'s own 60s
data-driven cooldown keeps serving the Ops path unchanged — two call
surfaces, two different abuse profiles, deliberately separate mechanisms.
`ResendTooSoon` → 429 with scope-accurate wording ("this user and
purpose", mirroring the Ops router's round-4 fix — the first draft said
"this address", wrong for bound calls where a prior send to a different
address for the same user+purpose also trips); `VerificationSendFailed`
→ 502. Response is the narrow create-ack shape; the `id` is the NEW
record's (resend supersedes the old row).

### Profile page UI (Profile Page.md §8.4)

Card directly under the delivery-email block: per record the email
(unmasked, §8.2's decision — the user already knows this address),
purpose label, status ("waiting for confirmation" / "delivery failed —
check the address, then resend"), resend button. Success calls
`router.refresh()` (re-fetches server data, preserves client state) —
**the first draft used `window.location.reload()` while its own comment
claimed `router.refresh()`** (review finding 4): a hard reload discards
client state across the whole page and the comment described a lighter
behavior than implemented. 429/503 map to user-readable wording per the
forgot-password error-state pattern. All strings i18n-keyed across
`frontend/src/locales/{en,zh-Hans,zh-Hant}.json` — note the catalogs
contain single-line compact structures (`"tags": [...]`, `"options":
{...}`) that a naive `json.dump(indent=2)` round-trip expands, which
buried this PR's first pass in ~1100 lines of pure reformatting noise
(review finding 5); the catalogs were restored to main's exact
formatting with only the new keys spliced in textually (strict JSON: no
trailing comma on the final inserted key).

### Out of scope (unchanged, per the issue)

§4.2 delivery-email write path, §3.6 report gating
(`recipient_email()`/`active_user_ids()` untouched), §3.7 unsubscribe.
Existing users get no automatic email — one-time backfill is a manual
Ops-API loop at the product owner's discretion. Released in v0.10.0;
not yet deployed at merge time.

## Report-email unsubscribe — issue #257

Design: Obsidian `Hermes/Portfonia/Docs/Ring 1-Email Validation.md` §3.7.
This is the deferred `revoked` status + confirm-page flow `86b7be7f1fe5`
called out as not-yet-implemented.

**Token**: stateless HMAC over
`email-unsubscribe-v1:{user_id}:{purpose}:{email}:{expires_unix}`, keyed
with `APP_SECRET_KEY` (same key as Altcha, no new secret), 7-day expiry
embedded in the signature. `create_token` / `verify_token` live in
`app/services/unsubscribe_token.py`; verify returns claims or `None`,
never raises.

**Send path**: `recipient_email_with_purpose()` tells `send_report_email`
which field it resolved so the token's `purpose` matches. Every report
email now sends Resend `text` (the markdown body plus a locale-keyed
unsubscribe footer) alongside `html`, and custom headers
`List-Unsubscribe: <https://…/unsubscribe?token=…>` plus
`List-Unsubscribe-Post: List-Unsubscribe=One-Click`. The confirm-page
flow is **not** RFC 8058 one-click compliant (it requires a page visit +
button click); emitting `List-Unsubscribe-Post` anyway is an accepted
deliverability simplification, not a hidden gap. A true one-click POST
path is still out of scope (design doc §七).

**Confirm**: `GET /unsubscribe/status` decodes the token only (no DB,
no writes). `POST /unsubscribe/confirm` (token only, no Altcha) clears
`users.*_verified_at` on every column whose *current* value equals the
token's email (so the same mailbox on both account and delivery fields
is fully revoked), and **appends** a single `status=revoked`
`email_verifications` row for the token's purpose — the historical
`verified` row is left untouched. Frontend `/unsubscribe` mirrors
`/verify-email` (Server Component GET + Server Action POST) and is in
`PUBLIC_PATH_PREFIXES`. Confirm-page copy talks about revoking
verification, not about delivery already having stopped: `send_report_email`
still sends to `recipient_email_with_purpose()` with no `*_verified_at`
check until issue #276.

**Idempotency**: the unsubscribe token's `now` is the end of the current
24h UTC bucket (Resend Idempotency-Key TTL), so a retry one second later
reuses the same html_body hash. `verify_token` swallows all exceptions
(including Python 3.12 `hmac.compare_digest` TypeError on a non-ASCII
digest) and returns `None`, matching Altcha.

**Still out of scope**: report-generation gating on verified status
(issue #276) — this PR's user-facing copy must not claim delivery has
already stopped; re-subscribe is "verify again via Profile".

## Report-generation / send gating on verified addresses — issue #276

Design: Obsidian `Hermes/Portfonia/Docs/Ring 1-Email Validation.md` §3.6,
implemented 2026-08-31 from the pre-implementation design comment on the
issue (comment 5487141241) — no design decisions were made during
implementation. This closes the gating gap every earlier entry in this
file records as deferred: `recipient_email()` "still ignores verification
state entirely" (#260 entry above) is no longer true.

**Two layers, both required** (a generated-but-unsendable report is pure
LLM spend; a sent-but-unverified delivery is the leak):

1. **Generation-time gate** — `active_user_ids()`
   (`app/services/user_scope.py`) now requires, for EVERY cadence,
   `email_verified_at IS NOT NULL OR delivery_email_verified_at IS NOT
   NULL`, AND'ed into the same `conditions` list as the existing
   `status`/`report_cadence` filters. Deliberately NOT scoped to
   `_HOLDINGS_GATED_CADENCES`: the holdings gate is a per-cadence content
   tradeoff (an empty weekly book still gets the empty-table contract),
   while "nowhere to deliver" is undeliverable regardless of cadence —
   hence unconditional. Test coverage: unverified user excluded on both
   mwf and weekly; either single timestamp satisfies the gate; the mwf
   holdings gate keeps AND'ing on top (verified but empty-book mwf user
   still excluded by the other condition).
2. **Send-time gate** — `recipient_email_with_purpose()`
   (`app/services/user_directory.py`): an address only counts when its
   OWN `*_verified_at` is set. Verified `delivery_email` > verified
   `email` (same preference order as before); an unverified
   `delivery_email` no longer rides on a verified account email or vice
   versa, and both-unverified means `None` (fail closed).
   `recipient_email()` is unchanged — thin wrapper. Tests: the two
   pre-existing resolution tests now pass explicit verified timestamps
   (expected change under the new rule, not a regression); new tests pin
   the no-cross-fallback rule (delivery_email set + unverified, email
   verified → resolves to `account_email`) and the both-unverified →
   `None` case.

**`None` split into two ops alerts** (`send_report_email`,
`app/services/email_sender.py`): the `resolved is None` branch re-checks
the user row (`session.get`) on this exceptional path only — missing or
non-active row keeps the original
`"Portfonia: report recipient could not be resolved"` subject (bug
signal, unchanged); active-but-no-verified-address fires the new
`"Portfonia ops: report has no verified recipient"` subject. Both stay
as email alerts per the design doc — observe real trigger frequency
while the user base is small; downgrading the second to a log line is a
recorded future step, explicitly not built here.

That second alert's body was corrected in PR #288's review round
(blacktomb42, CHANGES_REQUESTED): the first draft said "no report was
generated for send", which is false on every path that can reach the
branch — `send_report_email` only ever runs AFTER a `Report` row exists
and delivery was refused. The realistic triggers are (1) admin /
self-service generate of an unverified user (the exemption Layer 2
exists to cover) and (2) the fan-out-time-verified /
send-time-unverified race from design doc §3.6; a scheduled
never-verified user is excluded by Layer 1 and never reaches this
function at all. The body now states "generated report was NOT emailed
(email_sent_at left null)" with those two expected causes. The review
also noted the ERROR log above the split still conflates both `None`
causes, the Welcome/Profile copy, and the Ops API's "successful
generate emails the user" reference — accepted as follow-up, out of
this PR's stated scope but now factually stale. Test coverage: two new
tests run the REAL resolver against a real `User` row through
`db_session` (the pre-existing tests mock `recipient_email_with_purpose`
at module boundary, which cannot reach this branch by construction) and
assert the subject per case; after the review round the
no-verified-recipient test also asserts the body says "generated, not
emailed" and never "no report was generated" — subject-only assertions
were what let the wrong body ship green in the first place.

**Admin manual-generate needs no exemption code**
(`POST /admin/users/{user_id}/reports/generate`): it resolves the user
via `session.get(User, user_id)` and never calls `active_user_ids()`, so
ops can still force a generation run for an unverified user to diagnose
"why no reports" — the #221 §2.7 exemption precedent on this endpoint
extends to the new gate for free (verified by reading the router, not by
adding code). Send-time Layer 2 still applies downstream, so the forced
report persists but is not emailed and the new no-verified-recipient
alert fires — matching the issue's intent.

**Fixture/footprint note (the one thing the design comment did not
predict)**: the new generation gate turned every unverified fixture user
invisible to the fan-out — 19 pre-existing integration tests
(`test_shared_compute_a1`–`a4` via conftest's `three_user_holdings`, plus
`test_weekly_cadence_fanout`) flipped to `no_active_users`/empty
fan-outs. Their users are stand-ins for EXISTING books (the fixture
docstring says so), so the fix was stamping those fixtures'
`email_verified_at`, mirroring the same expected-change treatment the
user_directory tests got — not a weakening of the new rule, whose own
tests use unverified-by-default helpers.

## Wording unification + no-verified-recipient self-service recovery — issue #289

Design: Obsidian `Hermes/Portfonia/Docs/Ring 1-Email Validation.md`
(2026-08-31 section) + `Ring 1-Profile Page.md` §10, implemented from
the issue's design comment (5487631850) and the two design docs. Three
independent items; the first two are copy-only, the third adds one
endpoint plus Profile UI.

1. **Report email footer copy** (`app/services/email_sender.py`
   `_UNSUBSCRIBE_FOOTER_COPY`, `en`/`zh`): expanded from the
   "Revoke verification for this address" one-liners to explain what the
   report is, that it was delivered per the user's own configured
   settings, and what the link does to future delivery. Register is plain
   "unsubscribe" — kept consistent with the /unsubscribe page. The
   footer may name Portfonia (it is a Portfonia report email); the
   page-side copy stays generic ("this platform") so Vigil's future reuse
   of the same page shape needs no rewrite — wording constraint only, no
   multi-tenant plumbing built.
2. **/unsubscribe page register** (`unsubscribe-form.tsx` + the
   `unsubscribe` keys in all three locale catalogs): heading/button
   already said "Unsubscribe" while the body said "Revoke verification"
   — unified to unsubscribe language end to end. Page copy is generic
   ("reports and verification emails from this platform"), not
   Portfonia-report-specific. Locale files were edited text-level, never
   `json.dump(indent=2)`-reformatted (PR #263's ~1100-line noise-diff
   trap).
3. **`POST /email-verifications`** (`app/routers/email_verification.py`):
   session-authenticated (`Depends(current_principal)`) creation of a
   fresh verification for one of the caller's OWN known fields —
   purpose=account_email resolves `users.email`, purpose=delivery_email
   resolves `users.delivery_email` (422 when unset). The request has no
   `email` field; an extra client-supplied address is ignored, never
   used. Allowed when the target is already verified — create_verification
   supersedes only live `pending` records, so `*_verified_at` is
   untouched (mirrors the Ops API's "resend doesn't unverify" behavior,
   Profile Page.md §9.8). No new service logic: calls the shared
   `create_verification` used by resend/signup-hook/Ops. Rate limiting
   reuses `rate_limit_enforce_resend_verification` verbatim (same
   per-user 3/h + global per-address 3/h Redis buckets, sha256-bucketed,
   fail-closed 503) — deliberately not a separate allowance, per the
   issue's design comment. Response is the narrow `{id, status,
   expires_at}`, never the plaintext token. This is the self-service
   recovery path for the dead-account gap: after `verified → revoked`
   (email unsubscribe) no pending record remains to resend (§8.3), and
   the only prior remedy was an ops-token-gated `POST
   /admin/email-verifications`.
4. **Profile gap card** (`profile-page-body.tsx` +
   `use-verification-send.ts` + `lib/api.ts`): the `noVerifiedRecipient`
   state now lists every address on record — `email` plus `delivery_email`
   when set (today at most two; Vigil's own emails stay off this surface)
   — each with a "Send verification" button calling the new endpoint.
   Success uses `router.refresh()` (a fresh `GET /me`), never
   `window.location.reload()` — the same mistake PR #263's review caught
   once already. New i18n keys in all three catalogs; 503 reuses the
   resend flow's fail-closed wording.

**Test coverage**: new `test_email_verification_create.py` (auth 401;
both purposes resolve the server-side address; delivery unset → 422;
invalid purpose (incl. ops_manual) → 422; client-supplied email ignored;
already-verified untouched; supersede of prior pending; 429/502 mapping;
shared 3/h limiter trips on the 4th call). `test_email_sender.py` pins
the new en/zh footer copy. `unsubscribe-form.test.tsx` pins the unified
register. `profile-page-body.test.tsx` pins the gap-card buttons, the
purpose passed per row, refresh-on-success and translated 429/503/other
errors; the `deliveryCard()` helper now disambiguates the delivery card
title from the gap card's purpose label (both render "Report delivery
email" when nothing is verified — intended duplication).
