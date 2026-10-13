# Personal agent API access (#651, #652)

A user's own agent can read reports on every plan and complete daily holding
snapshots, intelligence and current holdings on an active Advanced plan (Daily or Jade).
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
values return 422. Holdings calls require an active Advanced or Jade subscription (#754);
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
6. Endpoint entitlement, via `is_advanced(user)` for snapshots, intelligence and holdings (403
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

## Current holdings management (#754)

The owner-selected product note is the existing `AI_Agent.md` in the project
Docs folder. It owns product decisions; this mechanism records implementation.
The #753-#755 batch remains subject to separate merge, migration and deployment
authorization. This PR implements #754; no production operation is implied.

### Transport and behavior

These routes manage the caller's current holdings records. They do not access
broker accounts or execute trades. GET and validation accept either token
permission; every mutation requires `holdings_write`. All holdings calls require
an active Advanced or Jade subscription. Report/snapshot/intelligence routes
remain read-only. Cached valuation preview and the Agent-only OpenAPI discovery
URL are not available yet; use this reference for the implemented routes.

| Method / path | Body and result |
| --- | --- |
| `GET /agent/v1/holdings` | No body. 200 complete book. |
| `PUT /agent/v1/holdings` | `{version, holdings, delete_ids}`. 200 complete book. Idempotency-Key required. |
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
fields only. Required fields cannot be null. Empty PATCH is a no-op. New single
rows append; single updates retain order. Whole and single changes are atomic,
including account resolution. Normalized no-ops retain their version and
system timestamps. Manual creation/value changes update the manual timestamp.

Every write requires the current canonical nonnegative integer string version
(`"0"`, `"1"`, etc.), in the body except DELETE's quoted If-Match header.
Do not combine version sources. A stale version returns 409 version_conflict
with `current_version`; reread and reconcile, rather than blindly replacing
the version in an old payload. Basic/Advanced/Jade caps are 30/50/100 stored
rows, counting cash and watches. Existing over-limit books remain readable and
editable/reducible; operations introducing rows must end within the cap.

POST and whole PUT require a trimmed 1-128-character ASCII Idempotency-Key
using letters, digits, `.`, `_`, `:` or `-`. Keys are shared across the user's
tokens, independently for creation and complete replacement. Preserve the
original key, version and payload for a retry. Successful results are retained
for 24 absolute hours: identical canonical content returns the original status,
body, IDs and version with `Idempotency-Replayed: true`, before stale-version
checking. Decimal spelling/default omission and JSON key order are canonicalized;
array order remains significant. Different content under the same key returns
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
at rest and expire after 24 hours.

Current-holdings GET and snapshots share one access email per ET day. Changed
writes have at most one separate modification email per ET day. Validation,
no-op writes and replay send no data notice. A notification failure after commit
preserves the success and replay result. Notice links revoke all tokens without
sign-in/confirmation to stop future access; revocation does not undo saved changes.

Users build and schedule their own synchronization. Preserve other institutions'
rows, stable source/account/row-to-ID mapping and user-maintained fields absent
from a source download (watches, notes and grouping labels). Do not infer deletion
from failed, incomplete, empty or stale downloads, or guess an ambiguous ID.
The platform version detects platform edits, not source-file freshness. After a
lost POST/whole-PUT response, retry the original payload/key within 24 hours,
then reread and reconcile. Outside retention, reconcile before another mutation.

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
explicitly bypasses legacy normalization. No-op writes leave timestamps alone.
Post-commit captures use existing sparse ticker/fund helpers only for new rows
or changed instruments. Notification failures log only user/kind metadata.

Migration `d75400000001`, following `d75300000001`, adds `agent_idempotency`:
non-null user FK with ON DELETE CASCADE, operation, SHA-256 key/request hashes,
status, EncryptedString response JSON and created/expiry timestamps, with unique
user/operation/key_hash and an expiry index. Key reuse/lookup is serialized by
the user lock. The existing operational sweep deletes expired results in
1000-row batches and returns agent_idempotency_deleted; no new Beat entry.
User purge explicitly removes replay rows before user deletion, with unchanged
purge response counts. Read notices retain the snapshot marker; modification
notices use a separate ET-day marker and the existing verified-recipient, locale
and signed revoke-all mechanism, with one retry after 300 seconds. Emails remain
English/Simplified/Traditional; Spanish UI receives the English fallback.

### Remaining batch and backlog

#755 owns cached temporary valuation and the public Agent-only OpenAPI/schema
discovery package. Neither route/schema is made available by #754. E reorder
and F capability lookup remain backlog. No broker downloads/logins, source
reference/date persistence, server pipeline scheduling or trade execution is
implemented. The earlier docs-only creation-only retry proposal is superseded
by #754's owner-confirmed 24-hour POST and complete-PUT idempotency contract.
