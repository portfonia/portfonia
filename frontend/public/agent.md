# Portfonia personal agent API

Base URL: `https://api.portfonia.com`.
This personal API lets an agent read its user's data and manage current holdings. It provides
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

An active Advanced subscription is required (Daily or Jade). Both dates
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

Only successful snapshot reads trigger a notice email, at most once per ET day.
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

## Current holdings (Advanced or Jade)

These routes manage the caller's current holdings records. They do not access
broker accounts or execute trades. GET and validation accept either token
permission; every mutation requires `holdings_write`. All holdings calls require
an active Advanced or Jade subscription. Report/snapshot/intelligence routes
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

Only successful snapshot reads trigger an access email, at most once per ET day.
Every successful Agent write request, including unchanged writes, may trigger
a separate daily modification email describing the write request. Current-holdings
GET, validation and idempotent replay send no data notice. A notification failure after commit
preserves the success and replay result. Notice links revoke all tokens without
sign-in/confirmation to stop future access; revocation does not undo saved changes.

## Limits, errors and token security

Limits apply per account across all of its tokens, only to agent endpoints:

- 20 requests per endpoint per hour (fixed window).
- More than 10 requests across endpoints within one minute locks token
  access for 15 minutes.
- Single-row holdings POST/PUT/PATCH/DELETE use a separate
  shared limit of 30 per minute and 100 per hour instead of the two limits
  above. Request 31 in one minute locks all token access for 15 minutes.
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
Tokens are shown only once when created.
Keep them secret and revoke any token at the
[AI Agent page](https://portfonia.com/agent).

Requests are audited with time, user/token identifiers, endpoint, requested
dates, status, item count, IP and User-Agent. Audit records are retained for
90 days. Plaintext credentials, holding values and intelligence text are
not recorded in this request audit.

Holdings and related intelligence are private. Handle them only on the
user's behalf, under the user's instructions, and keep their token private.
