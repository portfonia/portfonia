# Portfonia personal agent API

Base URL: `https://api.portfonia.com`.
This read-only API lets an agent access its user's own data. It provides
information and context, not investment advice. Create a token at
[AI Agent](https://portfonia.com/agent), then send:

```http
Authorization: Bearer pfa_...
```

Personal tokens authenticate only `/agent/v1/*`. Browser session JWTs do
not authenticate these endpoints. All endpoints share the authentication,
limits and request audit described below. No request generates a report,
collects intelligence or calls an LLM.

## Reports (all plans)

`GET /agent/v1/reports?start=YYYY-MM-DD&end=YYYY-MM-DD`

Both dates are required, inclusive and interpreted in Eastern Time (ET,
America/New_York). `start` must be on or before `end`; `end` must not be
later than today ET. Missing, malformed, reversed or future dates return
422. There is no maximum range length.

Returns the caller's available reports (`success` or `skipped`) ordered by
`report_date` descending, then creation time descending. At most the five
newest reports in the range are returned. `total_in_range` counts all
available reports in the range; `truncated` is true when that count exceeds
five. Reports under review, still generating or failed are excluded.

Response schema (date and timestamp fields serialize as ISO strings):

```text
{
  start: date, end: date, total_in_range: integer, truncated: boolean,
  items: [{
    id: UUID, report_date: date,
    type: "daily" | "mwf" | "weekly" | "manual" | "other",
    status: "success" | "skipped",
    period_start: timestamp | null, period_end: timestamp | null,
    generated_at: timestamp | null, report_md: string | null
  }]
}
```

`type` maps the stored session node: `daily_close` to `daily`, `after_close`
to `mwf`, `weekend_snapshot` to `weekly`, `manual` to `manual`, otherwise
`other`. Markdown is stored in the language chosen by the user at report
generation; this API does not translate it.

Example: `GET /agent/v1/reports?start=2026-09-01&end=2026-10-04`:

```json
{
  "start": "2026-09-01", "end": "2026-10-04",
  "total_in_range": 12, "truncated": true,
  "items": [
    {"id": "00000000-0000-0000-0000-000000000001", "report_date": "2026-10-04", "type": "daily", "status": "success", "period_start": null, "period_end": null, "generated_at": "2026-10-04T17:10:00-04:00", "report_md": "## Holdings context\n\nExample briefing."}
  ]
}
```

The example abbreviates the five returned items to one. To request older
reports, set `end` before the oldest returned `report_date`.

## Holding snapshots (Advanced)

`GET /agent/v1/snapshots?start=YYYY-MM-DD&end=YYYY-MM-DD`

An active Advanced subscription is required (currently Daily). Both dates
are required and inclusive, `start <= end`, `end <= today ET`, and the range
must cover at most 30 calendar days. Invalid parameters return 422;
insufficient entitlement returns 403 `subscription_required`.

Only complete daily batches belonging to the caller are returned. A
completed day with no holdings has `base_currency: null` and `holdings: []`.
Incomplete or unavailable days are omitted. Monetary values and shares
serialize as decimal strings; unavailable values are null.

```text
{
  start: date, end: date,
  days: [{date: date, base_currency: string | null, holdings: [{
    holding_id: UUID | null, ticker: string | null, fund_code: string | null,
    market: string | null, broker: string | null, account: string | null,
    portfolio: string | null, asset_class: string | null,
    pricing_mode: string | null, currency: string, shares: decimal | null,
    current_value: decimal | null, market_value: decimal | null,
    market_value_base: decimal | null, cost_basis_base: decimal | null,
    fx_rate_used: decimal | null, price_as_of: date | null,
    fx_as_of: date | null, data_quality: string
  }]}]
}
```

Example: `GET /agent/v1/snapshots?start=2026-10-04&end=2026-10-04`:

```json
{"start": "2026-10-04", "end": "2026-10-04", "days": []}
```

A successful snapshot read triggers a notice email at most once per ET day.
The notice includes a link that immediately revokes all the user's tokens,
without sign-in or confirmation. Reports and intelligence do not trigger
this notice.

## Intelligence (Advanced)

`GET /agent/v1/intel?date=YYYY-MM-DD`

An active Advanced subscription is required. `date` is required and must
be within the last seven ET calendar days including today:
`today ET - 6 days <= date <= today ET`. Missing, malformed, too old or
future dates return 422; insufficient entitlement returns 403
`subscription_required`.

Current holdings with a ticker are normalized to intelligence identifiers,
de-duplicated and read in holding position order. Funds without a ticker
are omitted. Headlines match the ET publication date, newest first; no
previously-surfaced filter is applied. Headline title and summary URLs are
removed at response projection only. Article bodies are accepted stored
records linked to intel slot runs with `run_date == date`, ordered newest
fetch first. Records lacking a string title or body are omitted.

Articles are globally de-duplicated by article id and URL key, including
macro articles. An article shared by several holdings appears only under
the first identifier in holding position order. Identifiers with neither
headlines nor articles are omitted.

Macro themes come from the caller's latest `success` or `skipped` report
on or before the requested date (latest creation time breaks same-date
ties), de-duplicated in stored order. Macro articles match those themes and
that date's slot runs. No qualifying report means `macro: null`. If the
latest report has no themes, `macro` names that report and `themes: []`;
the API does not search older reports. Themes without articles remain with
`articles: []`.

```text
{
  date: date,
  holdings: [{identifier: string,
    headlines: [{title: string, published_at: timestamp, summary: string | null}],
    articles: [{title: string, published_at: string | null, body: string}]
  }],
  macro: null | {
    source_report_id: UUID, source_report_date: date,
    themes: [{theme: string,
      articles: [{title: string, published_at: string | null, body: string}]
    }]
  }
}
```

Example: `GET /agent/v1/intel?date=2026-10-04`:

```json
{
  "date": "2026-10-04",
  "holdings": [{"identifier": "NVDA", "headlines": [{"title": "Company update", "published_at": "2026-10-04T12:00:00-04:00", "summary": null}], "articles": []}],
  "macro": {"source_report_id": "00000000-0000-0000-0000-000000000001", "source_report_date": "2026-10-03", "themes": []}
}
```

Intelligence carries no source links, URL keys, provider or publisher
fields. Use the supplied text; do not infer or invent source attribution.

## Planned holdings management: strict contract (not available yet)

The following owner-approved requirements describe a future release. Current
Agent endpoints remain read-only. Do not call planned holdings routes or
assume a current token has write permission. This section does not announce
availability; consult the implemented endpoint documentation when released.

The planned workflow lets users download their institution's holdings and
use their own agent to update Portfonia. Users manage downloads, scheduling,
source credentials and source-to-Portfonia holding-ID mappings themselves.

The holdings contract must enforce these rules:

- Complete submissions use `{version, holdings, delete_ids}`. Every current
  holding ID must occur exactly once in either the retained holdings or the
  explicit deletion list. Missing IDs reject the entire request; omission
  never means deletion. An ID cannot occur in both lists. New rows have no ID.
- `holdings` must not be empty. A complete submission cannot clear the book,
  even when every old ID appears in `delete_ids`. Use the web UI to clear it.
- Single-row DELETE must reject deletion of the last holding. Do not use a
  loop of deletions or an empty replacement to work around this restriction.
- Retained/new holdings follow the submitted array order. IDs are stable;
  identical instruments in different accounts are not automatically merged.
- Only user-input fields are writable. IDs, timestamps, captured prices,
  internal account identifiers, capture support and automatic `asset_class`
  remain server-owned. Watch tiers must survive read-edit-submit.
- All API holdings writes require the version returned by a current read.
  A conflict means reread and reassess the changes; do not blindly substitute
  a new version and repeat an old payload. Writes succeed or roll back together.
- Creation requires an idempotency key. Successful results are retained for
  24 hours; matching retries return the original result, changed content under
  the same key is a conflict. Preserve the key and original request on retries.
- Holdings calls require holdings-write permission and an active Advanced or
  Jade subscription. Permission is chosen when the token is created and cannot
  be changed; create a new token and revoke the old one. New tokens default to
  read-only. A holdings-write token can be created now. Holdings routes and
  their schema are not available yet.
- When single-row holdings writes are available, POST `/agent/v1/holdings` and
  PUT, PATCH and DELETE `/agent/v1/holdings/{holding_id}` share 30 requests per
  minute and 100 per hour across the account's tokens. Request 31 in one minute
  locks all token access for 15 minutes. Those calls do not use the older
  10-per-minute or 20-per-endpoint-hour limits. The routes are not available yet.
- Web and file saves enforce 30 rows on Briefing, 50 while Advanced is active,
  and 100 while Jade is active. Cash and watched rows count. Weekly, Mon/Wed/Fri
  and inactive subscriptions use 30. Rows already over the limit stay. Reads,
  edits and reductions remain allowed. Additions are refused until the book is
  within the cap. Agent holdings routes are not available yet.

For a complete submission with existing rows A/B/C, retaining A/B and explicitly
deleting C is valid; retaining only A and deleting C rejects because B is
unaccounted for. A/B/C symbolize UUIDs, not valid request identifiers. To edit
A alone, use the future single-row PATCH operation instead of a complete PUT.

A pipeline must preserve other institutions' rows in complete submissions.
Do not infer deletion from a failed, incomplete, empty or stale source download.
Keep a stable holding-ID mapping rather than matching by ticker alone. The
source download may omit watch tiers, notes or grouping labels that the user
maintains in Portfonia: preserve those current values unless the user's workflow
explicitly changes them. Do not replace absent source fields with null/defaults.
When mapping is ambiguous, stop the affected update instead of guessing an ID.
The
platform version protects concurrent platform edits, not source-file freshness.
After an ambiguous timeout of a complete submission, reread and reconcile
before sending another mutation. The planned 24-hour creation idempotency
does not establish idempotency for complete submissions.

The future Agent-only OpenAPI contract must describe these requirements and
errors explicitly, including `minItems: 1` on the complete holdings array.
Database-dependent checks still require server enforcement. A temporary
valuation endpoint and complete-submission idempotency are proposals only;
neither is approved or available.

## Limits, errors and token security

Limits apply per account across all of its tokens, only to agent endpoints:

- 20 requests per endpoint per hour (fixed window).
- More than 10 requests across endpoints within one minute locks token
  access for 15 minutes.
- When single-row holdings writes are available, those calls use a separate
  shared limit of 30 per minute and 100 per hour instead of the two limits
  above. Request 31 in one minute locks all token access for 15 minutes.
  The holdings routes are not available yet.
- Requests pause from five minutes before a heavy scheduled job's scheduled
  time until five minutes after it finishes. Heavy jobs include intelligence
  collection and scheduled briefings. Retries keep access paused. A schedule
  window also covers five minutes after the scheduled time while publication
  is pending.

Authentication precedes limits; limits precede entitlement and parameter
validation. On 429, wait for `Retry-After` seconds before retrying. Do not
attempt to bypass account limits by switching tokens.

| HTTP status | Meaning |
| --- | --- |
| 401 | Missing, unknown, expired or revoked token; inactive user; or session JWT used instead of an API token (`unauthorized`). |
| 403 | Advanced subscription required (`subscription_required`). |
| 422 | Missing or invalid date parameters or an invalid range. |
| 429 | Request limit, burst lock or scheduled quiet window (`too many attempts, try again later`); includes positive `Retry-After` seconds. |
| 503 | Rate-limit protection is unavailable; access fails closed (`temporarily unavailable`). |

At most five active tokens are allowed. A token unused for 60 days expires
automatically; optional chosen expiry dates end at 23:59:59 ET. Each new token
is read-only unless holdings write is chosen at creation. That choice is fixed.
Holdings routes are not available yet. Tokens are shown only once when created.
Keep them secret and revoke any token at the
[AI Agent page](https://portfonia.com/agent).

Requests are audited with time, user/token identifiers, endpoint, requested
dates, status, item count, IP and User-Agent. Audit records are retained for
90 days. Plaintext credentials, holding values and intelligence text are
not recorded in this request audit.

Holdings and related intelligence are private. Handle them only on the
user's behalf, under the user's instructions, and keep their token private.
