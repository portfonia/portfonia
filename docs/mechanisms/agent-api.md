# Personal agent API access (#651, #652)

A user's own agent can read reports on every plan and complete daily holding
snapshots and intelligence on an active Advanced plan (Daily or Jade).
The public `/agent` page explains all three endpoints and provides token
settings after session verification. Get started links to it for guests and
signed-in users. `/llms.txt` points to the English `/agent.md` API reference
and the human guide; both static files are public without a session. The
API base URL is `https://api.portfonia.com`; authentication is
`Authorization: Bearer pfa_...`.

## Tokens and authentication

Migration `d64200000001`, after `d64100000001`, adds only `api_tokens` and
`api_audit_log`. It adds no dependency or Settings field and performs no
existing-data mutation. Downgrade drops these two tables and their records.

Session-only `GET/POST /me/api-tokens` and
`DELETE /me/api-tokens/{token_id}` use unchanged `current_principal`.
Create accepts a trimmed 1–50-character name and optional `expires_on` date.
The date must be after today ET; expiry is 23:59:59 ET on that date. Tokens
are `pfa_` plus `secrets.token_urlsafe(32)`. Only SHA-256 and the first 12
characters are persisted. The plaintext is returned once by creation (201),
and the browser keeps it only in the mounted component's state.

Creation locks the user's row to enforce at most five active credentials.
The sixth returns 409 `token_limit`. List omits revoked tokens and reports
`active`, `expired` or `expired_unused`, newest first. A credential is active
only if it is not revoked, its optional expiry is strictly in the future,
and its last use (creation if never used) is at most 60 days ago. Expiry is
calculated at read/auth time; no cleanup task is needed. Revoking another
user's or an already-revoked token returns 404.

`agent_principal` accepts only `pfa_` bearer credentials and active users.
It records known token ownership in request state before rejecting expired
or revoked credentials, then commits `last_used_at` for accepted credentials.
Browser idle and absolute lifetime checks are not called. API tokens cannot
authenticate web or Ops routes; session JWTs cannot authenticate agent routes.

Issue #753 adds non-null `api_tokens.permission`, constrained to `read_only`
or `holdings_write`, default and backfill `read_only` (migration
`d75300000001`). Create accepts optional `permission` and defaults to
`read_only`. List and create responses include `permission`. There is no
route that changes it. Existing rows stay read-only. Any signed-in user may
create either mode. `AgentPrincipal` carries the stored permission. Unknown
values return 422. Holdings Agent routes are not registered; when they are,
holdings calls still require an active Advanced or Jade subscription.

## Request limits and scheduled quiet windows

The agent router evaluates these checks after authentication and before
query validation or endpoint entitlement:

1. Quiet window.
2. Existing `agent:lock:{user_id}` (remaining TTL).
3. For the future single-row pairs `POST /agent/v1/holdings` and
   `PUT`/`PATCH`/`DELETE /agent/v1/holdings/{holding_id}` only (#753):
   `agent:single:minute:{user_id}` allows 30 per 60 seconds; request 31 sets
   the same 900-second account lock. `agent:single:hour:{user_id}` allows 100
   per 3600 seconds and returns 429 with the remaining TTL on request 101.
   These calls do not increment the burst or per-endpoint hour counters.
   The routes are not registered.
4. Otherwise, `agent:burst:{user_id}`: fixed 60-second window from its first
   increment; the eleventh request sets a 900-second lock and returns 429.
5. Otherwise, `agent:hour:{endpoint}:{user_id}`: 20 per 3600-second fixed window.
6. Endpoint entitlement, via `is_advanced(user)` for snapshots and intelligence (403
   `subscription_required`).

Limits combine all tokens belonging to the same user. Every 429 has a
positive `Retry-After`; a protecting Redis failure returns 503. Web and
Ops limits retain their existing behavior. The existing counter backend's
Redis and memory implementations also support expiring marker writes and
prefix existence checks.

`API_QUIET_BEAT_ENTRIES` explicitly declares all 50 Beat entries as booleans.
The two intel slots, weekday/weekly report batches and Jade history fill are
true. Its
test rejects undeclared entries and non-booleans. Schedule edits require the
owner-confirmed declaration in the issue Design; no runtime default is used.

The existing connected `before_task_publish` handler sets
`agent:quiet:active:{task_id}` for heavy task names with a four-hour TTL.
The connected `task_postrun` handler does nothing on `RETRY`, preserving the
same-id retry's active marker; other states delete it and set a five-minute
cooldown. Redis errors log without failing the task.

Request-time checks first inspect active markers (`Retry-After: 300`), then
the cooldown's TTL. Without either, each heavy entry's cron is copied and
searched from five minutes before now, with ET-pinned `nowfun`. Celery's
strict search includes the lower boundary by anchoring one microsecond
before it. Absolute duration arithmetic uses UTC, then the ET schedule.
If the fire time is within five minutes after now, block through five
minutes after that fire (at least one second). No clock time is hard-coded.

For the weekday 17:00 ET report batch: 16:56 gives 540 seconds; 17:00:03
before publication gives 297; 17:05:01 without markers passes. A running
batch stays blocked by its active marker, including retry delay/run; its
terminal completion starts another 300 seconds of cooldown.

## Snapshots and request audit

Web and agent routes call `portfolio_snapshot_export.export_snapshot_range`.
Both preserve #643's caller-only complete batches, 30-day inclusive range,
ET future-date checks, encrypted ORM reads, decimal strings, empty-day null
currency and exact payload. The web route retains its own session, access
and limit dependencies. No agent request collects data or calls an LLM.

HTTP middleware applies only to the `/agent/v1` prefix. It records one row
with the actual response status, including 401/403/404/405/422/429/503.
Unhandled exceptions record 500 and propagate. Matched routes use the route
template; otherwise the raw path is truncated to 200 characters. Only sent
`start`/`end`/`date` parameters are recorded. Metadata also includes time, nullable
user/token ids, 12-character bearer prefix, item count (snapshot days, reports, or intelligence items), peer IP and
User-Agent (maximum 512 characters). NUL characters in endpoints, date
parameters and User-Agent are replaced with U+FFFD before persistence. Audit
write failures remain fail-closed. No holding values or full credentials
are logged. Known expired/revoked credentials retain their attribution.

For 404/405, where authentication did not run, middleware performs one
read-only hash lookup without updating `last_used_at`. Missing/unknown
credentials have null user and token ids. Non-agent requests write no rows.
The existing `cleanup_operational_events` task also removes audit rows older
than 90 days in 1000-row batches; it gains `api_audit_deleted` in its result
and adds no Beat entry. Purge deletes audit rows before tokens and the user,
reporting both counts in the existing Ops purge response.

## Report and intelligence pull endpoints (#652)

Both routes live on the existing agent router and reuse `agent_principal`,
`agent_limits` and the audit middleware. No migration, dependency or Settings
field is added. These requests read stored data only and enqueue no access
notice. They do not change report generation, collection or web report pages.

### Reports: all token users

`GET /agent/v1/reports?start=YYYY-MM-DD&end=YYYY-MM-DD` requires both inclusive
ET dates, `start <= end`, and `end <= today_et()`; invalid dates/ranges return
422. There is no maximum range length. Read only the caller's `success` and
`skipped` reports, ordered by `report_date desc, created_at desc`, limited to
five. Response `AgentReportsOut` contains `start`, `end`, `total_in_range`,
`truncated` (`total_in_range > 5`), and `items`. Each item contains `id`,
`report_date`, `type`, `status`, nullable `period_start`, `period_end`,
`generated_at`, and nullable stored `report_md`. No report inputs or email
HTML are exposed. Markdown remains in the language chosen at generation.

Map `session_node`: `daily_close` to `daily`, `after_close` to `mwf`,
`weekend_snapshot` to `weekly`, `manual` to `manual`, otherwise `other`.
Audit `item_count` is the returned report count. Twelve available reports
produce five items, `total_in_range: 12`, `truncated: true`. Request older
reports by setting `end` before the oldest returned report date.

### Intelligence: Advanced

`GET /agent/v1/intel?date=YYYY-MM-DD` uses `is_advanced` (403
`subscription_required`). The required date must satisfy
`today_et() - 6 days <= date <= today_et()`; otherwise return 422.

Load only the caller's current holdings with a ticker, in `position` order;
normalize via `intelligence_identifier(InstrumentKey("ticker", ticker))`
and de-duplicate identifiers. Funds without a ticker are excluded. Headlines
join `NewsInstrument` by identifier and match the ET publication date,
newest first, without a `news_surfaced` filter. Project `title`,
`published_at`, nullable `summary`; apply `intel_body.without_urls` to title
and summary in this response only. Stored headlines remain unchanged.

Accepted articles join `IntelArticleLink` and `IntelSlotRun.run_date == date`,
newest `fetched_at` first. Project only string `record.title`, string
`record.body`, and string-or-null `record.published_at`; skip invalid title
or body types. Global article-id and URL-key sets cover the whole response,
including macro. A shared holding article belongs only to the first
identifier in holding position order. This matches Pass 2's global
de-duplication, deliberately not its anomaly/weight attribution order.
Identifiers with neither headlines nor articles are omitted.

Select the caller's latest `success`/`skipped` report with
`report_date <= date`, latest creation time breaking same-date ties. Read
unique `report_inputs.macro_signals.hits[*].theme` values in stored order.
Accepted articles for each theme use the same date's slot runs and global
de-duplication. No qualifying report returns `macro: null`. A latest report
without themes (including quiet-day skipped reports) returns that report's
id/date and `themes: []`; never search older reports for themes. Themes with
no returned article retain `articles: []`.

Response `AgentIntelOut` is `{date, holdings: [{identifier, headlines,
articles}], macro}`; macro is null or `{source_report_id,
source_report_date, themes: [{theme, articles}]}`. Article fields are
`title`, nullable `published_at`, and `body`. No URL, URL key, provider,
publisher or source field is projected. Audit `item_count` counts all
headlines plus holding and macro articles; audit params include `date`
with the existing NUL replacement.

Example: current holdings NVDA, 0700.HK and a tickerless fund, latest report
2026-10-05 with theme `us_rates`, request 2026-10-06: include the two tickers'
that-date material, omit the fund, and return `source_report_date:
"2026-10-05"` with `us_rates` articles from 2026-10-06 slot runs.

The AI Agent page reuses D2's intro, limits, quiet-window, notice and token
rule keys, adds only Getting started and Endpoints copy under `agent.docs`,
and links `/llms.txt` and `/agent.md`. English is authoritative; Simplified
and Traditional Chinese additions are implementation-authored for owner
review. Agent-readable docs describe 401 authentication failures, 403
entitlement, 422 validation, 429 with positive `Retry-After`, and 503
protection unavailability. All dates are ET and holdings data remains
private, handled on the user's behalf.

## Access notices and signed revocation

After a successful snapshot read, an atomic
`agent:notice:{user_id}:{et_date}` SET NX claim (129600-second TTL) enqueues
`send_api_access_notice_task` once per ET day. The worker resolves a verified
delivery email first, then verified account email; unresolved recipients
are logged and skipped. `_API_ACCESS_NOTICE_COPY` supplies English,
Simplified and Traditional Chinese. The notice contains no holding values
or API credential; it explains the read and offers a revoke-all link.
The sender returns delivery success to the bound task, which retries once
after 300 seconds on failure (`max_retries=1`). A second failure emits one
ERROR log containing the user id, without the email address or link.

The link signs `api-token-revoke-v1:{user_id}:{expires_unix}` using the
existing APP_SECRET_KEY HMAC and unsubscribe encoding, valid for 30 days.
Public `POST /api-tokens/revoke-by-link` accepts `{token}` and returns
`{revoked_count}`. It revokes all non-revoked credentials with
`revoked_by=email_link`; replay returns zero. Invalid/tampered/expired links
return 400 `invalid_link`. GET never changes credentials.

Public `/agent/revoke?t=...` automatically posts once per mount, including
React StrictMode effect replay, and displays success or invalid-link text.
It requires no sign-in or confirmation and sends no further email.

## Ops and legal copy

Both endpoints use the existing independent Ops bearer and audit wrapper:

- `GET /admin/users/{user_id}/api-audit?start=&end=`: inclusive ET dates,
  ordered range of at most 31 days, newest first (id breaks timestamp ties),
  at most 1000 rows with a `truncated` flag.
- `POST /admin/users/{user_id}/api-tokens/revoke-all`: returns the count and
  records `revoked_by=ops`.

Unknown users return 404. Responses contain only audit metadata or a count;
no holdings, snapshots, reports or intelligence are exposed.
Privacy gains section 7, AI Agent access; later sections shift by one.
Terms adds token-holder duties to the end of section 6, Acceptable Use.
The Chinese copy is implementation-authored for owner review in the PR.

Review, merge, deployment and production data work require separate owner
instructions. Historical #651 delivery plan: deploy together with #650/#652,
and defer the Ops reference update to authorized post-deployment work.
The original implementation PR changed no Obsidian note or infrastructure.
That historical plan does not describe the status of the current Ops note.

## Product control and planned holdings management (2026-10-13)

The owner-selected Obsidian product-control note is `AI_Agent.md` in the
project's existing Docs folder. It owns user-facing Agent requirements,
decisions, design proposals, development status and the migrated stage-D
decision history. The Ring 2 development note keeps a stage index. The Ops
reference continues to own administrative audit and token-revocation calls.
This mechanism documents shipped code separately from planned behavior.

The owner approved the following next scope; none is implemented by this
documentation change, and no new issue has been opened:

| Capability | Proposed Agent route or protocol |
|---|---|
| Read all current holdings | `GET /agent/v1/holdings` |
| Submit the complete target holdings collection | `PUT /agent/v1/holdings` |
| Replace one holding's writable fields | `PUT /agent/v1/holdings/{id}` |
| Delete one holding | `DELETE /agent/v1/holdings/{id}` |
| Add one holding | `POST /agent/v1/holdings` |
| A: partial update | `PATCH /agent/v1/holdings/{id}` |
| B: validation without saving | `POST /agent/v1/holdings/validate` |
| C: conflict protection | Collection version returned on reads and required on writes |
| D: safe retries for creation | Idempotency key on holding creation |
| G: machine-readable Agent contract | Public Agent-only OpenAPI schema |
| Plan-specific holding caps | Basic 30, Advanced 50, Jade 100 rows; web and file writers enforce this (#753). Agent routes do not exist yet |

All current-holdings capabilities target active Advanced subscribers,
including Daily and Jade, using the existing `is_advanced` entitlement.
Token permission, the shared web/file writer, the collection version and the
single-row limit profile shipped in #753, described above and in the
subscription mechanism. Holdings routes, idempotency, valuation and the
OpenAPI schema did not. The approved write scope does not authorize token
calls to web or Ops routes.

The JSON path must preserve existing holding IDs and fully read/write
`watch_tier` (`null`, `watch`, `focus`, `critical`), including zero-share
watched rows. It cannot simply reuse file-import replacement: current
`confirm_holdings(mode="replace")` deletes/recreates IDs and strips
`watch_tier`. This extension does not itself change file-import semantics.
Use holding IDs rather than ticker identity; several rows may describe the
same instrument in different accounts.

### Confirmed decisions and remaining product questions

The owner answered the original six questions on 2026-10-13:

1. Reject an empty holdings array and direct the user to clear holdings in
   the UI. Deletions must be explicit in JSON; omission must never delete
   an existing ID. The confirmed envelope is `{version, holdings, delete_ids}`.
   Every old ID must appear exactly once in retained holdings or deletions;
   missing/overlapping IDs reject the entire transaction. Array order applies
   to retained/new rows. Single-row DELETE rejects deletion of the last row,
   with the same instruction to clear holdings in the UI. Serialize that check
   to prevent concurrent deletions from clearing the book.
2. Default new tokens to read-only; keep permissions immutable; allow all
   signed-in users to create either permission, while checking subscription
   entitlement on each holdings call.
3. The owner confirmed that all user-input fields are editable, with no
   automatic synchronization and uniqueness by holding ID. System fields
   (IDs, timestamps, captured prices, internal account IDs and capture support)
   remain server-owned. Automatic `asset_class` cannot be overridden.
   Decimal strings and unknown-field rejection belong to the authored technical
   schema proposal, whose exact format is to be specified in detailed design.
4. Make every web, import-confirm and Agent holdings mutation update the
   collection version; require the read version on all Agent writes. Web and
   file writers increment `users.holdings_version` once per successful change
   of normalized user fields, membership or order (#753). No-op normalized
   edits and price/NAV/FX capture do not. Agent reads and writes do not exist
   yet, so the version is not on a public holdings response.
5. Require an idempotency key for creation, retain successful results for
   24 hours, replay matching requests and reject changed content under the
   same key. Recognize committed replays before rejecting the original stale
   version; persist the creation and idempotency result in one transaction.
6. Share the existing daily snapshot-read notice with current-holdings GET;
   send at most one separate modification notice per day after successful
   writes. The owner confirmed a separate shared single-row write bucket of
   30 requests/minute and 100/hour, combining POST/PUT/PATCH/DELETE across all
   tokens of the user. These calls no longer consume the old global 10/minute
   bucket or the old 20/endpoint/hour quota. Request 31 within the minute
   triggers the existing 15-minute account lock; request 101 in the hour gets
   hourly 429/Retry-After. Other calls keep their existing limits, and the
   account-wide lock and heavy-task quiet windows still apply. The limit
   profile is enforced for those method and route templates (#753). The
   holdings routes, notices for holdings writes, and version-on-read contract
   are not implemented.

R1/R2/R3/R4 are all owner-confirmed. Reject omitted IDs in complete submissions: given
existing rows A/B/C, submit A/B plus `delete_ids: [C]` to retain/update A/B
and delete C. Submitting A plus `delete_ids: [C]` leaves B unexplained and
must reject the whole request without writing. Use single-row PATCH to
edit A alone. These symbolic IDs illustrate intent, not runnable UUID payloads.

Basic 30, active Advanced 50 and active Jade 100 holding rows are enforced by
`services/holdings_write.py` for web and file writers (#753). The count
includes every stored row, including watches and cash. Weekly, Mon/Wed/Fri,
no subscription, and expired, cancelled or inactive users use Basic. An
active cancel-pending subscription keeps the higher cap until expiry. Jade
is evaluated before Advanced. Rows already over the cap are not deleted;
reads, edits and reductions remain, and an addition, including a file replace
that rebuilds IDs, must finish at or under the cap. The refusal is HTTP 409
`holding_cap`. Agent holdings routes are not registered; when they are, they
use this same writer. See the subscription mechanism.

The authored validation proposal covers complete replacement, creation,
single-row replacement and PATCH. It applies the corresponding operation's
rules, returns normalization and field/row errors, and does not save holdings,
increment their version, enqueue captures or send access/modification notices.
Normal authentication bookkeeping, request limits and audit still apply.
Validation does not reserve a version or guarantee a later write succeeds.
Exact envelopes, DELETE response, error contracts and replay representation
must be finished before an implementation issue is ready.

The implementation flow should use shared holdings services rather than call
HTTP handlers that independently commit. After authentication, existing limits,
entitlement and write permission, validate input, lock the user, check the
version and row ownership, normalize/write the complete transaction and
increment the version. Commit before existing necessary capture enqueueing.
Stored snapshot history remains unchanged; JSON handling calls no parser LLM.

Affected modules include `models/api_token.py`, `services/api_tokens.py`,
`routers/api_tokens.py`, `core/agent_auth.py`, `routers/agent.py`, holding
schemas/services and web writers, migrations for permissions/versioning and
idempotency, and the Agent token UI. Preserve current instrument normalization,
account resolution, encrypted decimal handling, ordering and price invalidation.
This is holdings-record management, not broker integration or trade execution.

### Backlog, documentation and acceptance

E (a dedicated reorder endpoint) and F (current capability lookup) are owner-
selected backlog. G is now owner-approved first-release scope: publish a
machine-readable OpenAPI contract covering existing and new Agent routes,
excluding Ops, browser-session and internal routes. Public schema discovery
does not expose user data or authorize calls. Keep `/agent.md` for semantics
and worked flows, and link the schema from it and `/llms.txt`. Define its
public URL and exact schema/error models in the detailed implementation design;
do not reopen the full production `/openapi.json`.

Extend `/agent.md` with writable/read-only fields, types, null/default behavior,
decimal precision, stable IDs, ordering, complete/partial replacement, empty
collection semantics, validation, conflict rereads, idempotent retries and
errors. Include stock, fund, cash, manual valuation and watch-only examples.
Do not advertise unimplemented routes in current public documentation.
Public agent.md and llms.txt may carry an explicitly unavailable future-contract
section; they must retain current endpoint availability and prevent readers
from calling planned routes as though they were implemented. At implementation,
the human guide and Agent schema must repeat the strict deletion, completeness,
last-row, version, idempotency, permission and quota rules. Declare `minItems: 1`
in the schema and document cross-row/database checks and their errors; schema
alone does not enforce them.

### User-built synchronization pipelines and unselected proposals

The owner's goal is institution downloads processed by a user's own agent
on the user's schedule. Portfonia provides the personal API, not institution
login/download automation or integrations specific to an agent product.
Document stable source/account/row-to-holding-ID mapping, retaining other
institutions' rows and user-maintained fields absent from the source (watch
tiers, notes and grouping labels), checking source completeness/freshness, and validating
then reconciling writes. A platform version cannot detect an old broker file.
After an ambiguous bulk-write timeout, reread/reconcile rather than blindly
changing the version and retrying; creation idempotency does not cover PUT.

Optional, unselected proposals are recorded in the product note: temporary
valuation and complete-submission idempotency, and source-reference/freshness
metadata if client-side mapping/checks prove insufficient. Do not make these
first-release requirements without the owner's selection. Ambiguous row mapping
must stop the affected update rather than guess a holding ID.

A proposed `POST /agent/v1/holdings/value-preview` would price candidate rows
using stored closes/FX and supplied manual values without saving, capturing or
calling an LLM. The existing calculator excludes rows with missing prices/FX
from its total, so a preview must expose per-row coverage, dates and missing
reasons, a priced subtotal and a null complete total when incomplete. The
author proposes Advanced/Jade access, read-only-token support, plan-sized input,
a separate 10/user/hour quota with existing quiet windows/account locks and
metadata-only audit. Neither the endpoint nor its policy is owner-approved.

Acceptance must prove old/read-only tokens cannot write, subscription expiry
removes holdings access, IDs/watch tiers survive read-edit-submit, invalid
bulk input rolls back, foreign/duplicate IDs are rejected, web edits conflict
with stale API writes, concurrent/retried creation yields one ID, validation
has no holdings side effects, and decimals retain precision. Also prove empty
bulk submissions cannot clear holdings, only explicitly listed IDs are deleted,
the Agent schema excludes private routes and matches the actual contract,
and plan caps/single-row limits follow the finalized R3/R4 boundary rules.
Run the project
code gates and independent review during implementation. No issue, code,
migration, merge, deployment or production action is authorized by this
docs-only delivery.

Public surfaces affected by the future implementation: Agent explanation and
token creation/list UI in all four UI locales, `/agent.md`, `/llms.txt`, the
Agent-only schema, and Pricing/Terms/Privacy or other inventory statements
that describe Agent access or holding caps (verify the
inventory during detailed design). This documentation PR changes no public
capability claims.
