# Personal agent API access (#651, #652)

A user's own agent can read reports on every plan and complete daily holding
snapshots and intelligence on an active Advanced plan (Daily or Jade). Current holdings,
validation, writes and Agent change-history reads require active Jade (#775).
Issue #754 adds holdings management with a holdings-write token and read-only
validation. See the current-holdings contract below.
The public `/agent` page explains the implemented endpoints and provides token
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
values return 422. Holdings calls require an active Jade subscription (#754);
mutations additionally require holdings_write permission.

## Request limits and scheduled quiet windows

The agent router evaluates these checks after authentication and before
query validation or endpoint entitlement:

1. Quiet window.
2. Existing `agent:lock:{user_id}` (remaining TTL).
3. For the single-row pairs `POST /agent/v1/holdings` and
   `PUT`/`PATCH`/`DELETE /agent/v1/holdings/{holding_id}` only (#753):
   `agent:single:minute:{user_id}` allows 30 per 60 seconds; request 31 sets
   the same 900-second account lock. `agent:single:hour:{user_id}` allows 100
   per 3600 seconds and returns 429 with the remaining TTL on request 101.
   These calls do not increment the burst or per-endpoint hour counters.
4. Otherwise, `agent:burst:{user_id}`: fixed 60-second window from its first
   increment; the eleventh request sets a 900-second lock and returns 429.
5. Otherwise, `agent:hour:{endpoint}:{user_id}`: 20 per 3600-second fixed window.
6. Endpoint entitlement, via `is_advanced(user)` for snapshots and intelligence, and
   `is_jade(user)` for holdings, validation, writes and change history (403
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
`start`/`end`/`date`/`since` parameters are recorded. Metadata also includes time, nullable
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

### Intelligence: Advanced headlines and Jade full detail (#781)

`GET /agent/v1/intel` keeps `is_advanced` entitlement (403
`subscription_required`). The route passes `is_jade(principal.user)` to
`pull_intel`; non-Jade gets `detail: "headlines"`, null summaries and empty
article arrays, without executing article queries. Jade gets `detail: "full"`
and retains the existing headline/summary and article projection.

Exactly one of `date` and offset-aware `since` is required (422 otherwise).
Date mode retains `today_et() - 6 days <= date <= today_et()`. Since mode
normalizes to ET, rejects values before midnight ET on the oldest date or
after now, and selects `[since, until)`. Compute in UTC:
`until = max(since, min(since + 24 hours, now - 5 minutes))`.
Return ET `since` and `next_since = until`; `has_more` is true when
`until < now - 5 minutes`. This uses elapsed hours across DST: October 31,
2026 at 12:00 EDT advances to November 1 at 11:00 EST. Clients store
`next_since`, call again with it and repeat while `has_more`.

The caller's current ticker holdings are normalized using
`intelligence_identifier(InstrumentKey("ticker", ticker))`, de-duplicated
in position order; tickerless funds are excluded. Direct (`relation IS NULL`)
headlines join `NewsInstrument`, use publication date in date mode and
`NewsInstrument.created_at` in since mode, and sort by publication descending.
Project `title`, `published_at`, nullable `summary`, stripping title/summary
URLs only in the response. Jade articles keep accepted/link filters and
newest `fetched_at` ordering, valid string title/body checks, nullable string
publication time and whole-response id/URL-key de-duplication. Date mode
uses slot `run_date`; since mode uses `finished_at` and excludes running
slots. Holdings with neither headlines nor articles are omitted.

Macro source selection remains the latest caller-owned success/skipped
report on or before `date`, or the window end's ET date, with creation time
breaking ties. Its unique themes retain stored order; no report returns
`macro: null`, and a latest report without themes returns `themes: []`.
Both tiers gain free pool headlines: select `News.origin == "pool"` by
publication date or, in since mode, `News.created_at`. Pool promotion in
`store_headline` updates both `origin` and `created_at = now()` so newly
promoted rows are included. Pass rows through `headline_from_row`,
`macro_news_items` (exclude commentary/off_topic), then `detect_macro_signals`
with `max_articles_per_theme=max(len(items), 1)`. Retain only source-report
themes, with newest-first headlines; strip URLs and suppress Advanced
summaries. Headlines can belong to multiple themes and are not de-duplicated
against holding headlines. Jade macro articles keep whole-response
de-duplication; empty themes retain both empty arrays.

`AgentIntelOut` adds `detail`, nullable `since`, `next_since`, `has_more`,
and nullable `date`; macro themes add `headlines`. Date mode has null cursor
fields, and since mode has null `date`. No source fields are exposed.
Audit counts all holding and macro headlines/articles and records raw `since`
with the existing NUL sanitization. Token capabilities, limits, quiet windows
and notices are unchanged; intel reads send no notice. The public schema,
`agent.md`, intel card and Pricing comparison/legal text document the tiers
and modes in the same PR. Advanced users receive no downgrade notification.

The AI Agent page reuses D2's intro, limits, quiet-window, notice and token
rule keys, adds only Getting started and Endpoints copy under `agent.docs`,
and links `/llms.txt` and `/agent.md`. English is authoritative; Simplified
and Traditional Chinese additions are implementation-authored for owner
review. Agent-readable docs describe 401 authentication failures, 403
entitlement, 422 validation, 429 with positive `Retry-After`, and 503
protection unavailability. All dates are ET and holdings data remains
private, handled on the user's behalf.

Issue #766 replaces the Endpoints card with a summary for a person. It opens
with one lead sentence, then one block per endpoint group in a fixed order.
#766 shipped ten blocks; #769 adds the token and change-history blocks and
#770 the Jade block, so the card now has thirteen. Each block is a heading
plus four unmarked lines: path, who may call it, what it does, and any other
note. A closing sentence follows. Paths have no query string, and
the card does not show the OpenAPI URL. English matches the #766 Design
comment. Spanish and both Chinese catalogs translate those sentences.
`llms.txt` and `agent.md` still hold the field lists and calling conventions,
including `total_in_range` and `truncated` on report reads, which the page
summary does not mention.

## Access notices and signed revocation

After a successful snapshot, current-holdings or change-history read, an atomic
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

## Current holdings management (#754)

The owner-selected product note is the existing `AI_Agent.md` in the project
Docs folder. It owns product decisions; this mechanism records implementation.
The #753-#755 batch remains subject to separate merge, migration and deployment
authorization. This PR implements #754; no production operation is implied.

### Transport and behavior

These routes manage the caller's current holdings records. They do not access
broker accounts or execute trades. GET and validation accept either token
permission; every mutation requires `holdings_write`. All holdings calls require
an active Jade subscription. Report/snapshot/intelligence routes
remain read-only. The Agent-only OpenAPI schema is available at
[https://api.portfonia.com/agent/openapi.json](https://api.portfonia.com/agent/openapi.json).

| Method / path | Body and result |
| --- | --- |
| `GET /agent/v1/holdings` | No body. 200 complete book. |
| `PUT /agent/v1/holdings` | `{version, holdings, delete_ids}`. 200 complete book. |
| `POST /agent/v1/holdings` | `{version, holding}`. 201 `{version, holding}`. Idempotency-Key required. |
| `PUT /agent/v1/holdings/{holding_id}` | `{version, holding}`. 200 `{version, holding}`. |
| `PATCH /agent/v1/holdings/{holding_id}` | `{version, changes}`. 200 `{version, holding}`. |
| `DELETE /agent/v1/holdings/{holding_id}` | No body; `If-Match: "<version>"`. 200 `{version, deleted_id}`. |
| `POST /agent/v1/holdings/validate` | Discriminated operation below. 200 validation result; no saving. |

A complete book is `{version: string, holdings: [record], metadata: [metadata]}`.
Each record contains its stable UUID `id` plus the writable fields below.
Metadata is separate, keyed by `id`: `asset_class`, `capture_supported`,
`last_manual_update` (timestamp/null), `created_at`, `updated_at` and
`position` (integer/null). `holdings` can be reused directly in complete PUT;
never copy `metadata` into the write body. Empty GET is legal. Same tickers in
separate accounts retain separate IDs; zero-share watched rows remain included.

Required writable fields: nonempty string `name`, supported uppercase `currency`
and `pricing_mode` (`auto` or `manual`). Optional fields default to null:
`ticker`, `fund_code`, `shares`, `avg_cost`, `current_value`, `asset_type`,
`market`, `broker`, `account`, `portfolio`, `notes`, `watch_tier`.
Unknown keys, system timestamps, captured prices, `asset_class`, capture support
and internal account IDs cannot be submitted. Optional identity/grouping/notes
fields are strings/null. Currency accepts USD, CNY, CNH, HKD, GBP, EUR, JPY,
SGD, AUD, CAD, CHF, KRW, TWD, MOP and NZD. `asset_type` is stock/etf/fund/cash/wmf/other
or null; `market` is US/HK/A-Share/UK/Europe/Japan/Korea/Other or null;
`watch_tier` is watch/focus/critical or null.

Amounts are null or JSON strings matching `^(0|[1-9][0-9]*)(\.[0-9]+)?$`.
Numbers, signs, exponent notation, NaN and infinity are rejected. Values pass
directly through Decimal and encrypted storage without float conversion.
Responses use fixed-point decimal strings, with fractional trailing zeros
removed. Cash and WMF must have manual pricing, a non-null current_value and
no ticker or fund_code. Other missing inputs are not fabricated. Existing
instrument/market/currency normalization applies; identity changes clear stale
stored prices. System capture does not change the holdings-input version.

Complete PUT requires every existing ID exactly once in `holdings` or
`delete_ids`. Lists must be unique and disjoint. Omission never deletes:
A/B/C -> holdings A/B + delete C succeeds; A + delete C fails because B is
unaccounted for, with no writes. Retained rows keep their IDs; new rows omit
`id` or set it null. Submitted array order sets positions 0..n-1. At least
one holding is required. Single DELETE cannot remove the last row; clear in
the browser UI. Ownership errors never disclose another user's row existence.

Single PUT replaces all writable fields and clears omitted optional fields to
null/defaults. PATCH retains omitted fields; explicit null clears nullable
fields only. Required fields cannot be null. Empty PATCH preserves stored fields,
including legacy values and prices, but increments the version. New single
rows append; single updates retain order. Whole and single changes are atomic,
including account resolution. Every successful mutation increments the version
once, including unchanged writes. Manual creation/value changes update the manual timestamp.

Every write requires the current canonical nonnegative integer string version
(`"0"`, `"1"`, etc.), in the body except DELETE's quoted If-Match header.
Do not combine version sources. A stale version returns 409 version_conflict
with `current_version`; reread and reconcile, rather than blindly replacing
the version in an old payload. Basic/Advanced/Jade caps are 30/50/100 stored
rows, counting cash and watches. Existing over-limit books remain readable and
editable/reducible; operations introducing rows must end within the cap.

Only POST creation requires a trimmed 1-128-character ASCII Idempotency-Key
using letters, digits, `.`, `_`, `:` or `-`. Keys are shared across the user's
tokens. Preserve the original key, version and payload for a retry. Within
24 absolute hours, identical canonical content returns the original status,
body, IDs and version with `Idempotency-Replayed: true`, before stale-version
checking. Decimal spelling/default omission and JSON key order are canonicalized.
Different content under the same key returns
409 idempotency_conflict. At exact expiry the key can be reused. Failed attempts
do not reserve a key. Authentication, current entitlement and limits still
apply to replay. Its old version is intentional; reread before another write.

Validation accepts exactly one of:

```text
{operation:"replace_all", version, holdings, delete_ids}
{operation:"create", version, holding}
{operation:"replace", version, holding_id, holding}
{operation:"patch", version, holding_id, changes}
```

It returns `{valid:true, version, normalized:<same operation shape>}` or
`{valid:false, version, errors:[{code,path,message}]}` for domain errors,
including stale version, cap, incomplete collection and unavailable IDs.
The response version is the current version. PATCH normalization returns the
merged writable fields (empty changes remain empty). Structural malformed JSON,
unknown fields and invalid field types return 422. Validation reads a consistent
book but does not reserve its version or guarantee a later write. It writes no
holdings/accounts/idempotency result, increments no version, captures no prices
and sends no data notice. Normal auth bookkeeping, limits and audit still apply.

New holdings domain errors use `{detail:{code,message,...}}`:

| Status | Codes / additional fields |
| --- | --- |
| 403 | subscription_required, holdings_write_required |
| 404 | holding_not_found for absent or foreign IDs |
| 409 | version_conflict (current_version), idempotency_conflict, holding_limit (limit/current_count), last_holding_requires_ui |
| 422 | empty_holdings (clear in browser), incomplete_collection (owned missing_ids), duplicate_id, conflicting_id, invalid_holding, invalid_idempotency_key |
| 428 | version_required for DELETE without If-Match |

Existing 401/429/503 auth/protection and other endpoints' error shapes are
unchanged. GET/whole PUT audit counts returned rows; single operations count
one; validate counts candidate rows. Request audit never stores bodies, keys or
replay payloads. Successful replay bodies are stored separately with encryption
at rest; POST retries within 24 hours return the original result.

Successful reads of current holdings, holding change history and snapshots
share at most one access email per ET day. Every successful Agent write request,
including unchanged writes, may trigger a separate daily modification email.
Validation and idempotent replay send no data notice. A notification failure
after commit preserves the success and replay result. Notice links revoke every
API token on the account at once, including other agents' tokens. Create new
tokens afterwards for agents you still want to run. Revocation stops future
access and does not undo saved changes.

### Implementation boundaries

`schemas/agent_holdings.py` owns strict JSON transport. `routers/agent.py`
uses existing token-only auth, method/template limits, audit and entitlement;
old routes retain their errors. `services/agent_holdings.py` checks versions,
ownership, completeness, normalization and final caps under the shared user
lock. `services/holdings_write.py:apply_agent_book` resolves accounts and
applies retained/new/deleted rows without committing. Agent saves encrypted
replay in the same transaction and commits once; existing web/file writers
keep their error, clearing and manual-timestamp conventions. No file-import
replacement is used for Agent writes. Existing history/snapshots are unchanged.

GET/validate acquire a shared User-row lock for coherent reads; web/file/Agent
writers acquire the same row exclusively. Mutation inputs use Decimal directly;
the parser LLM and ParsedRow's float amounts are not involved. Empty PATCH
explicitly bypasses legacy normalization while incrementing the version once.
Post-commit captures use existing sparse ticker/fund helpers only for new rows
or changed instruments. Notification failures log only user/kind metadata.

Migration `d75400000001`, following `d75300000001`, adds `agent_idempotency`:
non-null user FK with ON DELETE CASCADE, operation, SHA-256 key/request hashes,
status, EncryptedString response JSON and created/expiry timestamps, with unique
user/operation/key_hash and an expiry index. Key reuse/lookup is serialized by
the user lock. Lookup removes an expired record when its POST key is reused.
Account deletion removes records through the FK cascade; there is no retention
sweep or explicit purge deletion. Read notices retain the snapshot marker; modification
notices use a separate ET-day marker and the existing verified-recipient, locale
and signed revoke-all mechanism, with one retry after 300 seconds. Emails remain
English/Simplified/Traditional; Spanish UI receives the English fallback.

### Remaining batch and backlog

#755 publishes the public Agent-only OpenAPI schema at
[https://api.portfonia.com/agent/openapi.json](https://api.portfonia.com/agent/openapi.json). E reorder
remains backlog; #769 implements token capability lookup. No broker downloads/logins, source
reference/date persistence, server pipeline scheduling or trade execution is
implemented. The earlier docs-only creation-only retry proposal is superseded
by the 24-hour POST creation retry contract; whole PUT has no idempotency replay.

## Token information

`GET /agent/v1/token` checks the current token without a paid-subscription
requirement or data notice. It uses normal read limits, quiet windows and request
audit. Returns `name`, the stored 12-character `prefix`, `permission`, ET ISO 8601
`created_at`, nullable ET `expires_at`, and `capabilities` booleans. `reports` is
always true; `snapshots` and `intel` require active Advanced (Daily or Jade).
`holdings_read` requires active Jade; `holdings_write` requires active Jade
and the token's `holdings_write` permission.
Change history follows `holdings_read`. No quota or plaintext token is returned.

## Holding change history

`GET /agent/v1/holdings/changes?start=YYYY-MM-DD&end=YYYY-MM-DD` requires active
Jade and accepts either token permission. Both inclusive ET dates
are required: `today ET - 29 days <= start <= end <= today ET`; invalid ranges
return 422. Reads use normal read limits and share the daily access notice with
current holdings, snapshots and Jade risk results. Audit item count is the returned group count.

Response: `{start, end, truncated, items}`. Up to 100 groups are returned, ordered
by time descending then ID descending; narrow the range if `truncated` is true.
Each group has `id`, ET ISO 8601 `changed_at`, `source` (`web`, `file`, `agent`, `rollback`),
nullable `token` (`{name, prefix}` for Agent writes), `rollback_to` (nullable ET ISO 8601 target change time), and `rows`. Each row has
`holding_id`, `holding_name`, `kind` (`created`, `updated`, `deleted`) and `fields`.
Each field has `field`, `before`, `after`: strings, integer position, or null.
Amounts are fixed-point decimal strings without fractional trailing zeros.

History records user-input changes from web, file upload, Agent writes and rollbacks,
including row positions. Captured prices and other system fields are excluded.
File replacement records old IDs as deleted and new IDs as created. Empty or
unchanged writes, failed writes and idempotent replays create no history group.
History is encrypted at rest, retained for 30 days and visible only to its user
and their Agent. The signed-in `/holdings` page links to the history page.

Migration `d76900000001`, after `d75400000001`, adds `holding_change_groups`.
The existing retention task also removes groups older than 30 days in 1000-row
batches and reports `holding_changes_deleted`; no Beat entry changes. User
removal cascades through the group FK. No backfill or production operation is
part of implementation. See the holdings pipeline for transaction hooks.

Issue #783 adds rollback on `/holdings/changes` for every signed-in user,
including changes from Agent writes after a downgrade. The web list adds
`rollback_available`; the Agent response does not. Reverse replay undoes the
selected group and every newer retained group, without the 100-group display
cap. An after-value mismatch blocks that group and older groups, preserving
unrecorded Ops changes. A rollback is recorded as one encrypted `rollback`
group with `rollback_to` equal to the selected group's timestamp and can itself
be rolled back.

`GET /holdings/changes/{group_id}/rollback` previews the target time, group
count, restored/removed/modified row counts and current `version`.
`POST` to the same path accepts `{version}` and returns 204. It holds the shared
book lock and returns 404 for a missing target or 409 for `version_conflict`,
`rollback_blocked` or `holding_cap`. A no-op writes nothing. The unchanged cap
rule uses `introduces_rows = restored > 0`. Changed rows retain original IDs
and restore all 16 recorded fields exactly. Only those rows recompute
`asset_class` and `capture_supported` from a defaults copy and resolve accounts
without archiving; identity changes clear captured prices. Unchanged rows
remain untouched. Success bumps the version and enqueues sparse capture for
restored or identity-changed rows, without email. No Agent rollback route exists.

Migration `d78300000001` follows `d76900000001`, widens the source CHECK and adds
nullable `rollback_to`. Downgrade deletes rollback groups before dropping the
column and restoring the CHECK. New groups use an insert-time timestamp under
the book lock; existing timestamps and the column default are unchanged.
No production migration or data operation is authorized by this implementation.

## Jade risk results (#770)

Five read-only `GET /agent/v1/jade/{replay,tail-risk,stress,style,tracking}`
routes require active Jade and accept either token permission. They reuse the
web response models, caller-scoped cached computations and report-currency
fallback. Stress requires one `scenario` and computes only that scenario;
web stress retains all five. The five routes share per-user 180-second and
ten-per-hour buckets after normal Agent limits. Authentication and limits
precede entitlement and query validation, so rejected entitlement/parameter
calls also consume the interval. Success shares the existing daily read notice.
`GET /agent/v1/token` adds `capabilities.jade` independently of token permission.
The Agent OpenAPI schema publishes the five GET paths, without web routes or
reference writes. The complete field/code reference is in public `agent.md`.
Both data-notice copies put the revoke-all URL on its own line with one blank
line before and after; the read copy also names Jade risk results. No migration,
new notice marker, provider call or result cache is introduced.

Issue #775 makes programmatic current-holdings reads, validation, writes and change
history Jade-only, updates token capabilities, and renames the public token card
"Verify token permissions". Snapshot and intelligence access remains Advanced.
The four-locale public Agent guide now explains the complete agent-driven holdings
pipeline; #774 completes the shared-access-notice lists on the Agent page.

## Large holdings-change alerts (#785)

Agent writes and file uploads (append or replace) enqueue asynchronous alert
assessment after their holdings transaction commits. Web single-row edits and
rollbacks do not alert; existing daily access and modification notices are
unchanged. Enqueue and evaluation failures are logged without changing write
results. Task payloads contain only user and change-group IDs (plus the claimed
flag on a delivery retry).

Assessment reads the target group, current holdings and this user's groups at
or after the target in one REPEATABLE READ transaction. History replay yields
the before and after books; blocked reconstruction skips the alert. Transient
holdings derive capture support using only `resolve_holding_market`, without
changing recorded fields or deriving asset classes. Both books use the same
latest captured closes for their instruments, FX rates and user base currency,
with the per-row valuation rules shared with `compute_portfolio`. Unpriced or
no-FX rows contribute nothing; auto rows have no market-price fallback here.

Any of these signals triggers: a net row decrease of at least 30%, a total
value decrease of at least 30%, or a total value increase of at least 100%.
Value signals are skipped when the before value is not positive. There is no
minimum book size. The first evaluation to claim
`holdings:alert:{user_id}:{write_et_date}` sends, using the target group's ET
date and a 129600-second Redis SET NX marker. Tasks may finish out of write
order. A failed send retries once after 300 seconds without reclaiming the
marker, including across midnight.

Verified recipients receive hand-written English, Simplified Chinese or
Traditional Chinese copy; Spanish falls back to English. The email lists the
signals, write time and source (file upload or Agent token name and prefix),
row counts and signed rounded value-change percentage when available. It
contains no amounts, currencies or instrument names. It links to holding change
history and explains restoring holdings to before the change. Agent alerts
also include a fresh revoke-all link and the existing revocation paragraph;
file alerts do not. Privacy section 7, the Agent notice, guide limits and public
`agent.md` describe the separate alert. No migration or Beat entry is added.
