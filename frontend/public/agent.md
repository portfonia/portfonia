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

Successful snapshot, current-holdings, change-history and Jade reads share a notice
email, at most once per ET day.
The notice includes a link that immediately revokes all the user's tokens,
without sign-in or confirmation. Reports and intelligence do not trigger
this notice.

## Intelligence (Advanced headlines; Jade full detail)

`GET /agent/v1/intel?date=YYYY-MM-DD` or
`GET /agent/v1/intel?since=2026-10-15T09:00:00%2D04:00`

An active Advanced (Daily or Jade) subscription is required; otherwise the
endpoint returns 403 `subscription_required`. Advanced receives
`detail: "headlines"`: headline titles and publication times, all summaries
`null`, and all article arrays empty. Jade receives `detail: "full"`, including
headline summaries and included article text for holdings and macro themes.
Neither mode triggers an access notice or collects new material.

Supply exactly one of `date` or `since`. Date mode accepts the last seven ET
calendar dates, including today (`today ET - 6 days <= date <= today ET`).
Since mode requires a timestamp with a UTC offset, no earlier than midnight
ET on the oldest accepted date and no later than now. Invalid parameters
return 422. URL-encode a positive offset's `+` as `%2B`.

Since mode selects newly ingested material in `[since, next_since)`. The
window ends at the earlier of 24 elapsed hours after `since` and five minutes
before now. Arithmetic uses UTC so the 24-hour cap also holds across DST;
timestamps are returned with ET offsets. If that end would precede `since`,
the window is empty and `next_since` equals `since`. `has_more` is true when
the 24-hour cap ended the window before the five-minute margin. Store
`next_since`, call again with it, repeat while `has_more`, and retain the
last cursor for the next pull.

Current holdings with a ticker are normalized to intelligence identifiers,
de-duplicated and read in holding position order. Funds without a ticker are
omitted. Holding headlines match the ET publication date in date mode, or
the time their direct instrument link was created in since mode; both are
ordered newest publication first, without a previously-surfaced filter.
Headline title and summary URLs are removed only in this response.

Jade articles are included stored records, newest fetch first, with string
titles and bodies. Date mode matches slot `run_date`; since mode matches slot
`finished_at` within the window and excludes running slots. Articles are
globally de-duplicated by id and URL key across holdings and macro themes.
A shared article appears only under the first holding identifier in position
order. Holdings with neither headlines nor articles are omitted.

Macro themes come from the caller's latest `success` or `skipped` report on
or before the requested date (or the window end's ET date in since mode),
with latest creation time breaking ties. Themes keep their stored order.
Both tiers receive stored pool headlines matched by the report macro
detector, using title and provider snippet keywords. Rows labeled `commentary`
or `off_topic` are excluded; `development` and unlabeled rows are eligible.
Date mode uses publication date; since mode uses pool ingestion time,
including the time an existing instrument row is promoted into the pool.
Only themes in that source report are returned. Headlines may appear under
several themes and are not de-duplicated against holding headlines. Themes
without material keep empty arrays. No qualifying report means `macro: null`;
a latest report without themes returns `themes: []`, without searching older
reports. Jade macro articles follow the same slot selection and global
article de-duplication as holding articles.

```text
{
  detail: "headlines" | "full", date: date | null,
  since: timestamp | null, next_since: timestamp | null, has_more: boolean | null,
  holdings: [{identifier: string,
    headlines: [{title: string, published_at: timestamp, summary: string | null}],
    articles: [{title: string, published_at: string | null, body: string}]
  }],
  macro: null | {
    source_report_id: UUID, source_report_date: date,
    themes: [{theme: string,
      headlines: [{title: string, published_at: timestamp, summary: string | null}],
      articles: [{title: string, published_at: string | null, body: string}]
    }]
  }
}
```

In date mode `since`, `next_since` and `has_more` are null; in since mode
`date` is null and `since` echoes the input normalized to ET.

Advanced example, `GET /agent/v1/intel?date=2026-10-15`:

```json
{"detail":"headlines","date":"2026-10-15","since":null,"next_since":null,"has_more":null,
 "holdings":[{"identifier":"NVDA","headlines":[{"title":"Company update","published_at":"2026-10-15T12:00:00-04:00","summary":null}],"articles":[]}],
 "macro":{"source_report_id":"00000000-0000-0000-0000-000000000001","source_report_date":"2026-10-15","themes":[{"theme":"Monetary policy","headlines":[],"articles":[]}]}}
```

Jade example, same request:

```json
{"detail":"full","date":"2026-10-15","since":null,"next_since":null,"has_more":null,
 "holdings":[{"identifier":"NVDA","headlines":[{"title":"Company update","published_at":"2026-10-15T12:00:00-04:00","summary":"Provider snippet"}],"articles":[{"title":"Company development","published_at":null,"body":"Included article text"}]}],
 "macro":{"source_report_id":"00000000-0000-0000-0000-000000000001","source_report_date":"2026-10-15","themes":[{"theme":"Monetary policy","headlines":[],"articles":[{"title":"Policy development","published_at":null,"body":"Included macro article text"}]}]}}
```

Intelligence carries no source links, URL keys, provider or publisher fields.
Use the supplied text; do not infer or invent source attribution.

## Current holdings (Jade)

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

Successful reads of current holdings, holding change history, snapshots and Jade risk results
share at most one access email per ET day. Every successful Agent write request,
including unchanged writes, may trigger a separate daily modification email.
Validation and idempotent replay send no data notice. A notification failure
after commit preserves the success and replay result. Notice links revoke every
API token on the account at once, including other agents' tokens. Create new
tokens afterwards for agents you still want to run. Revocation stops future
access and does not undo saved changes.

## Jade risk results (Jade)

An active Jade subscription is required; both `read_only` and `holdings_write`
tokens may read. These JSON-only endpoints return the caller's own results using
the same response models and values as the web tools. They read the shared
nightly price cache without fetching providers or saving results. Every series
is returned in full; there is no series-omission switch or CSV format. The
tracking result includes the saved reference components; reference writes and
a separate reference-read endpoint are not available to Agents.

| GET path | Query parameters |
| --- | --- |
| `/agent/v1/jade/replay` | `base_currency`, `benchmark`, `range` |
| `/agent/v1/jade/tail-risk` | `base_currency`, `benchmark` (fixed five-year history) |
| `/agent/v1/jade/stress` | Required `scenario`, `base_currency`, `benchmark` |
| `/agent/v1/jade/style` | `base_currency`, `benchmark` (fixed three-month window) |
| `/agent/v1/jade/tracking` | `base_currency`, `range` (uses the saved reference) |

`base_currency` defaults to your report currency, otherwise USD; allowed values
are USD, CNY, CNH, HKD, GBP, EUR, JPY, SGD, AUD, CAD, CHF, KRW, TWD, MOP and NZD.
`benchmark` defaults to `sp500`; choices are `sp500` (SPY), `dow30` (DIA),
`nasdaq` (ONEQ) and `csi300` (510300.SS). `range` defaults to `1Y`; choices are
`1M`, `3M`, `6M`, `YTD`, `1Y`, `3Y`, `5Y` (calendar spans).
Stress requires exactly one `scenario`: `dotcom_2000`, `gfc_2008`, `covid_2020`,
`rates_2022` or `tariffs_2025`. A missing or unknown scenario returns 422;
`scenarios` contains exactly the selected entry, never all five.

Across all five routes and all tokens of one user, the combined Jade limit is
one request every 180 seconds and at most ten per fixed 3600-second window.
These limits apply after the existing Agent burst and per-route hourly checks;
quiet windows and the account lock also apply. Jade refusals return 429 with
positive `Retry-After` seconds, without creating an account lock. The interval
is claimed before the hourly check, entitlement and query validation: a request
later rejected with 403, 422 or the Jade hourly 429 still consumes the interval.
An interval refusal does not extend that interval. Authentication precedes all
limits. Successful Jade reads share the once-per-ET-day access notice with
snapshot, current-holdings and change-history reads.

### Field reference and disclosure codes

Dates are `YYYY-MM-DD`. Ratios, returns, volatility and weights are decimal
strings with six decimals, where `"0.010000"` means 1%; `beta` has four decimals.
Display-currency amounts (`portfolio_value`, `var_amount`, `cvar_amount`,
`shock_amount`) have two decimals. Counts are JSON integers; flags are booleans.
Tracking component weights are integer percentages. Null means unavailable or
inapplicable, never an inferred zero. Names and symbols identify instruments;
disclosure decisions use the following codes and flags, without localized text.

Fields shared by replay, tail risk and style:

| Field | Meaning and availability |
| --- | --- |
| `status` | `no_holdings`: no positively valued holdings; `pending`: portfolio history is not ready or no holdings could be included; `insufficient`: too few usable return samples; `ok`: portfolio calculation is available. Replay needs ten returns; style also needs all basis histories attempted and 42 overlapping three-day returns. Tail risk inherits fixed-5Y replay status. |
| `base_currency` | Currency used for valuation and historical FX conversion. |
| `benchmark` | Requested benchmark code listed above. |
| `benchmark_symbol`, `benchmark_name` | Selected benchmark ETF identity. |
| `benchmark_status` | `pending`: cache not attempted; `unavailable`: attempted but history/FX or samples do not support a result; `ok`: benchmark calculation available. Portfolio results can remain available when the benchmark is not. |
| `window_start`, `window_end` | Requested calendar bounds ending on the latest cached S&P 500 session on or before today ET; style uses 3M and tail risk 5Y. |
| `first_valid_date` | First usable portfolio level date, or null. |
| `sample_count` | Replay/tail-risk valid daily return count; style overlapping three-day return count on common basis dates. |
| `coverage` | Coverage object below, retained even when calculations are unavailable. |

`coverage` is also returned by each stress scenario and tracking:

| Field | Meaning and availability |
| --- | --- |
| `own_share` | Included value share using own price or fund NAV history. |
| `head_proxy_share` | Share using beta-scaled proxy history before own history begins. |
| `proxy_share` | Share using a class/market ETF throughout. |
| `cash_share` | Share held constant in its own currency, then converted through historical FX. |
| `cash_assumed_share` | Share treated as cash (wealth-management products and applicable CNY/CNH bond funds without usable own history). |
| `approx_share_at_start` | Sum of head-proxy, proxy and cash-assumed shares at the start. |
| `data_quality` | True at approximation share >= 0.66; a disclosure flag, not a result suppression. |
| `pending_share` | Pending positive value divided by included plus pending value; null when no pending value. |

Included shares use included positive current value as denominator. If nothing
is included they are zero. Proxies can mask instrument-specific tail losses and
mechanically improve fitted/tracking similarity; they do not establish safety.

Replay adds `range`, `skipped_days` (sample-calendar dates lacking a complete
portfolio level), `points`, `metrics` and `holdings`. `points[]` has `date`,
`portfolio` and `benchmark`: cumulative returns from each series' first usable
value, with null benchmark values when unavailable. Points are empty unless the
portfolio is ok. `metrics` has nullable `portfolio` and `benchmark` objects:

| Metrics field | Meaning |
| --- | --- |
| `cumulative_return` | Last value / first value minus one. |
| `annualized_return` | Compound return annualized using actual calendar days and 365.25 days/year. |
| `annualized_vol` | Sample standard deviation of daily returns times sqrt(252). |
| `max_drawdown` | Most negative peak-to-trough return. |
| `max_drawdown_peak`, `max_drawdown_trough` | Corresponding dates, earliest ties. |
| `worst_day`, `worst_day_date` | Lowest daily return and its end date. |
| `worst_month`, `worst_month_label` | Lowest calendar-month return and `YYYY-MM`; both null for 1M. |

`holdings[]` in replay and stress contains:

| Field | Meaning and availability |
| --- | --- |
| `holding_id`, `name` | Caller-owned saved holding UUID and name. |
| `weight` | Included current-value share, null for excluded rows. |
| `method` | `own`: own adjusted ticker history; `fund_nav`: own total-return NAV; `head_proxy`: beta-scaled ETF fill before own history; `proxy`: ETF throughout; `cash`: cash in own currency; `cash_assumed`: cash approximation; `excluded`: not used in the calculation. |
| `excluded_reason` | Null for included rows. `unvalued`: no positive valuation; `watch_only`: watched row with zero shares; `pending`: required history not attempted; `data_unavailable`: attempted data or FX does not support inclusion. |
| `proxy_symbol`, `proxy_name` | Proxy identity when a proxy is selected; can remain set after exclusion. |
| `beta`, `beta_samples` | Head-proxy scaling coefficient and paired return sample count; null otherwise. Beta defaults to 1 when fewer than 60 pairs or no positive proxy variance. |
| `own_first_date` | Earliest usable own-history date, when known. |

Replay holdings also have `own_history_unavailable` (true when attempted own
history cannot be used), `own_vol` (own annualized volatility, null below ten
paired returns) and `proxy_segment_vol` (annualized volatility of the filled
segment, null below two proxy returns).
Stress holdings instead add `asset_class` (classification or null), `proxy_for`
(primary proxy symbol when a substitute is used, else null), and `price_only`
(true when the chosen proxy lacks dividend adjustment).

Tail risk adds:

| Field | Meaning and availability |
| --- | --- |
| `month_windows` | Number of overlapping 21-position monthly return windows. |
| `month_independent` | Approximate non-overlapping count: floor(daily returns / 21). |
| `portfolio_value` | Included current value in display currency, null unless ok. |
| `levels` | 95% and 99% confidence entries; empty unless portfolio status is ok. |
| `histogram` | Daily-return bins `{lower, upper, count}` in ascending order (0.5 percentage-point width); empty unless ok. Bounds are return ratios. |
| `tolerance_status` | `ok` for a valid questionnaire; `no_questionnaire` when its reference cannot be computed. |
| `proxy_understates` | True when proxy/head-proxy share is positive; proxies may understate instrument-specific tail losses. |

Each `levels[]` entry has `level` (95 or 99), `available` (95 available for an
ok portfolio, 99 requires 500 daily returns), `tail_days` and `tail_windows`
(the worst ceil(n * (100-level) / 100) daily/monthly observations, nullable when
unavailable). `daily` and `monthly` are nullable historical loss cells;
`normal_daily` and `normal_monthly` are nullable normal-distribution comparison
cells; `benchmark_daily` and `benchmark_monthly` are nullable historical
benchmark cells, independently gated by benchmark samples. Monthly cells need
monthly observations; normal monthly needs at least two. `reference_daily` and
`reference_monthly` are nullable questionnaire-implied normal VaR ratios; a
questionnaire reference can exist even when a confidence level is unavailable.
Each cell has `var` (loss boundary) and `cvar` (mean loss in the tail), plus
nullable `var_amount` and `cvar_amount` (only historical portfolio cells have
amounts). Positive means loss; a tail gain yields a negative loss ratio.

Stress's envelope has `base_currency`, requested `benchmark`, primary
`benchmark_symbol`, primary `benchmark_name`, and one-element `scenarios`.
Each scenario contains:

| Field | Meaning and availability |
| --- | --- |
| `id` | Selected scenario code. |
| `peak_date`, `trough_date` | Fixed historical episode peak and trough. |
| `window_start`, `window_end` | Scenario history bounds including context around the shock. |
| `status`, `first_valid_date`, `sample_count` | Portfolio availability, first usable date and valid return count, using the same status codes; ten returns needed for ok. |
| `portfolio_value` | Included current value; null unless ok. |
| `shock_return`, `shock_amount` | Peak-to-trough return and its display-currency amount; null unless ok. Negative is a loss. |
| `max_drawdown`, `max_drawdown_peak`, `max_drawdown_trough` | Worst drawdown over the full scenario window and dates; null unless ok. |
| `benchmark_symbol`, `benchmark_name` | Resolved scenario benchmark identity, including a substitute if needed. |
| `benchmark_status` | Same codes as replay; ok also requires usable peak and trough values. |
| `benchmark_shock_return`, `benchmark_max_drawdown` | Benchmark shock and window drawdown; null without ok portfolio and benchmark. |
| `contributions` | `{asset_class, contribution}` entries sorted by contribution then class; peak-value-normalized shock contributions, empty unless ok. |
| `points` | `{date, portfolio, benchmark}` full path of returns relative to the fixed peak value (not first-point rebasing); benchmark nullable, empty unless portfolio is ok. |
| `coverage`, `holdings`, `proxy_understates` | Objects/codes defined above; proxy-understatement flag is true for positive proxy/head-proxy share. |
| `substitutions` | Chosen alternatives `{primary, symbol, name, price_only}` for included holdings and an available benchmark; `primary` is the original ETF, `symbol` and `name` identify the substitute, `price_only` means no dividends. Empty when no alternatives were used. |
| `price_only` (holding/substitution), `benchmark_price_only` | Whether the selected holding proxy/substitution or scenario benchmark is a price-only index. |
| `price_index_symbols` | Sorted unique price-only proxy/available-benchmark symbols; empty when none. |
| `fx_source` | `standard` for normal cached FX in 2020/2022/2025; `fred` for the 2000/2008 FRED historical FX windows, including CNY-for-CNH and HKD-peg-for-MOP approximations. |

Style adds `horizon_days` (3), `min_samples` (42), nullable `portfolio` and
`benchmark_fit`, `points` and `proxy_inflates_fit`. Each fit has `weights` (all
13 `{symbol, weight}` basis ETF weights, nonnegative and summing to one before
serialization), nullable `r_squared` (1 - residual variance / return variance,
null for constant returns, possibly negative), `residual_vol` (annualized
unexplained volatility), and `low_fit` (true when non-null unrounded R² < 0.60).
Rounding each weight independently may change their displayed sum slightly.
`points[]` has `date`, `portfolio`, `style_mix`: cumulative returns of unchanged
holdings and the fitted fixed mix. Non-ok portfolio results have null fits and
empty points; benchmark fitting has its own availability gate.
`proxy_inflates_fit` is true when proxy/head-proxy share is positive, disclosing
that proxy history can inflate the fit. The fitted mix is a statistical
explanation, not actual holdings or a proposed allocation.

Tracking's complete fields are `status`, `range`, `base_currency`, `window_start`,
`window_end`, `first_valid_date` (inherited portfolio replay context),
`components`, `benchmark_saved`, `benchmark_status`, `sample_count`,
`horizon_days`, `min_samples`, `metrics`, `points`, `coverage`, `proxy_note`.
`components[]` has `symbol` and integer-percent `weight`, preserving saved
order; `benchmark_saved=false` selects the unsaved 60% SPY / 40% AGG example.
Reference choices are SPY, QQQ, DIA, ONEQ, IWF, IWD, IWM, ACWI, EFA, EEM, 2800.HK,
510300.SS, ISF.L, EXSA.DE, 1306.T, 069500.KS, AGG, TLT, BIL, GLD, DBC, VNQ, XLE,
and CASH (constant in display currency). They are read here, not editable.
`benchmark_status` is null until portfolio status is ok, then `pending` if any
reference series is unattempted, `unavailable` if common-date endpoint coverage
fails, or `ok`. `sample_count` counts overlapping five-position active returns;
`horizon_days=5`, `min_samples=10`. `metrics` is null and `points=[]` unless both
portfolio and reference are ok. Metrics fields are `portfolio_return`,
`benchmark_return` (daily constant-weight reference), `excess_return` (their
cumulative difference), `annualized_excess` (difference between separately
annualized returns) and nullable `tracking_error` (annualized sample deviation
of five-day active returns, null below ten samples). `points[]` has `date` and
`excess` (cumulative portfolio return minus cumulative reference return).
`proxy_note` is true when proxy/head-proxy share is positive. `coverage` follows
replay. Overlapping returns are not independent observations.

### Short response examples

These are JSON response excerpts: other envelope fields are omitted for
brevity. Series arrays are explicitly truncated to one illustrative point/bin
below; actual responses return the entire series. Values are illustrative,
not a user's results. Consult each guide for the method and limitations.

[Replay guide](https://portfonia.com/guides/jade-replay):

```json
{"range":"5Y","status":"ok","base_currency":"USD","points":[{"date":"2026-10-08","portfolio":"0.100000","benchmark":"0.120000"}]}
```

[Tail-risk guide](https://portfonia.com/guides/jade-var):

```json
{"status":"ok","portfolio_value":"10000.00","tolerance_status":"no_questionnaire","proxy_understates":false,"histogram":[{"lower":"-0.010000","upper":"-0.005000","count":12}]}
```

[Stress guide](https://portfonia.com/guides/jade-stress), `scenario=covid_2020`:

```json
{"base_currency":"USD","benchmark":"sp500","scenarios":[{"id":"covid_2020","status":"ok","shock_return":"-0.200000","shock_amount":"-2000.00","fx_source":"standard","points":[{"date":"2020-03-23","portfolio":"-0.200000","benchmark":"-0.300000"}]}]}
```

[Style guide](https://portfonia.com/guides/jade-style):

```json
{"status":"ok","horizon_days":3,"min_samples":42,"proxy_inflates_fit":false,"points":[{"date":"2026-10-08","portfolio":"0.040000","style_mix":"0.035000"}]}
```

[Tracking guide](https://portfonia.com/guides/jade-tracking):

```json
{"status":"ok","range":"1Y","components":[{"symbol":"SPY","weight":60},{"symbol":"AGG","weight":40}],"benchmark_saved":false,"benchmark_status":"ok","proxy_note":false,"points":[{"date":"2026-10-08","excess":"0.010000"}]}
```

## Verify token permissions

`GET /agent/v1/token` checks the current token without a paid-subscription
requirement or data notice. It uses normal read limits, quiet windows and request
audit. Returns `name`, the stored 12-character `prefix`, `permission`, ET ISO 8601
`created_at`, nullable ET `expires_at`, and `capabilities` booleans. `reports` is
always true; `snapshots` and `intel` require active Advanced (Daily or Jade).
`holdings_read` requires active Jade; `holdings_write` requires active Jade
and the token's `holdings_write` permission.
`jade` requires active Jade, regardless of token permission, and indicates
full intelligence detail; `intel` alone indicates headline access.
Change history follows `holdings_read`. No quota or plaintext token is returned.

## Holding change history

`GET /agent/v1/holdings/changes?start=YYYY-MM-DD&end=YYYY-MM-DD` requires active
Jade and accepts either token permission. Both inclusive ET dates
are required: `today ET - 29 days <= start <= end <= today ET`; invalid ranges
return 422. Reads use normal read limits and share the daily access notice with
current holdings, snapshots and Jade risk results. Audit item count is the returned group count.

Response: `{start, end, truncated, items}`. Up to 100 groups are returned, ordered
by time descending then ID descending; narrow the range if `truncated` is true.
Each group has `id`, ET ISO 8601 `changed_at`, `source` (`web`, `file`, `agent`),
nullable `token` (`{name, prefix}` for Agent writes), and `rows`. Each row has
`holding_id`, `holding_name`, `kind` (`created`, `updated`, `deleted`) and `fields`.
Each field has `field`, `before`, `after`: strings, integer position, or null.
Amounts are fixed-point decimal strings without fractional trailing zeros.

History records user-input changes from web, file upload and Agent writes,
including row positions. Captured prices and other system fields are excluded.
File replacement records old IDs as deleted and new IDs as created. Empty or
unchanged writes, failed writes and idempotent replays create no history group.
History is encrypted at rest, retained for 30 days and visible only to its user
and their Agent. The signed-in `/holdings` page links to the history page.

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
| 403 | Required subscription missing (`subscription_required`): Advanced for snapshots and intelligence, Jade for current holdings, change history and Jade results. |
| 422 | Missing or invalid parameters, including dates, ranges or Jade scenario. |
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
