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

## Request limits and scheduled quiet windows

The agent router evaluates these checks after authentication and before
query validation or endpoint entitlement:

1. Quiet window.
2. Existing `agent:lock:{user_id}` (remaining TTL).
3. `agent:burst:{user_id}`: fixed 60-second window from its first increment;
   the eleventh request sets a 900-second lock and returns 429.
4. `agent:hour:{endpoint}:{user_id}`: 20 per 3600-second fixed window.
5. Endpoint entitlement, via `is_advanced(user)` for snapshots and intelligence (403
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
instructions. Deploy together with #650/#652. The Ops reference note update
is deferred until authorized post-deployment work; this PR changes no Obsidian
note and no infrastructure.
