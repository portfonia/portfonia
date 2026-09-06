# Portfolio Performance — Phase 1 (backend only)

Issue #360. Governing decisions: the issue's Decisions comment + the
2026-09-06 amendment comment + the Implementation design comment (read
those before this file — this is an implementation summary, not the spec
itself). Paired Chinese-language design doc: Obsidian
`Hermes/Portfonia/Docs/Portfolio_Pfmc.md`.

## Scope

Phase 1 ships schema + two daily Celery tasks + two one-off backfill
scripts + `GET /portfolio/performance`. No frontend, no chart — Phase 2 is
a separate follow-up PR against this frozen response contract. Deliberately
does not touch `/portfolio/summary` or `compute_portfolio`'s
`capture_supported=False` exclusion (D5 amendment: Performance computes its
own value rules independently — aligning the two is explicitly out of
scope for this phase).

## Schema

- `portfolio_value_snapshots` — one row per holding per user per day.
  Denormalized, no FK to the live `holdings` row (a later edit/delete must
  not corrupt historical readability — same reasoning as `accounts`'
  broker/account/portfolio text columns). `holding_id` is a soft, nullable
  UUID (no FK) used only for day-to-day quantity alignment in the TWR calc.
  `user_id` is `ON DELETE CASCADE` — unlike holdings/reports/accounts
  (`RESTRICT`, issue #129 B7), this is derived time-series data, not an
  audited record, so a user purge needs no new step in
  `app/services/user_purge.py`.
- `portfolio_snapshot_batches` — per-(user, day) `pending|complete|
  skipped_deps` marker. The read API only ever considers `complete` days;
  `skipped_deps` means the day's FX dependency wasn't resolvable at write
  time and the day is silently retried on the next run rather than exposed
  half-computed.
- `benchmark_prices` — daily close for `sp500|dow30|nasdaq` (Nasdaq
  Composite, not the Nasdaq-100 — D9), unrelated to any user's holdings.

Migration: `c1d2e3f4a5b6_add_portfolio_performance_tables.py`.

## Valuation rules (D5 amendment)

Implemented in `app/services/portfolio_history.py`, independent of
`portfolio_calculator.compute_portfolio`:

- Auto-priced holdings: `shares × historical close` (`historical_price`,
  10-day lookback to bridge weekends/holidays).
- Cash/wmf and any manual-valued holding (including
  `capture_supported=False`): the stored `current_value` IS the local
  value — no price-return concept. Converting it to base currency still
  floats with FX (`fx_rate_used` recorded per row).
- No usable value at all → `data_quality="insufficient"`, `market_value_
  base=None` — never zero-padded.
- A day is written only if every FX pair the user's holdings need can be
  resolved for that date (10-day lookback); otherwise the whole day is
  marked `skipped_deps` and no rows are written for that user/day.

## Approximate EOD TWR (D3 amendment)

`app/services/portfolio_performance.py`'s `_contribution`: day *t*'s
return marks yesterday's *filtered* holdings at *today's own stored row*
for the same `holding_id` — `unit_value_base = today.market_value_base /
today.shares` (auto) or `fx_multiplier = today.market_value_base /
today.current_value` (cash/manual), multiplied by yesterday's quantity/
local-value. A holding with no row on day *t* (exited, or relabeled out of
the current filter) is simply excluded from that day's numerator — the
denominator (`V_{t-1}`) still includes it, so an exit or an out-of-filter
relabel reads as an implicit cash flow, never a return. This only needs
two adjacent snapshot rows; it never re-queries `price_snapshots`/
`fx_rates` at read time.

TWR off: `(V_end / V_start) − 1` over the range's first/last included days
— literally the raw market-value ratio, which is why the header's dollar
amount (`value_change_base`) is always framed as "market value change"
regardless of the `twr` toggle (D9 / the issue's requirement 7).

## Filters (D8)

`markets`/`groups`(`portfolio`)/`brokers`/`accounts`, each multi-select,
AND'd across dimensions, applied to each day's own denormalized labels —
a sold lot or a since-renamed account/broker still appears in the days
before the change. `portfolio.empty=true` only when literally no snapshot
row in the selected range matches the filter at all; an empty *current*
book with matching history still draws that history.

## Currency

Each row's `market_value_base` is in the user's own persisted
`users.base_currency` at capture time (the "canonical" currency) — not
re-derived per request. If the request's `base_currency` differs, the
already-aggregated per-day totals are re-converted once per day via
`historical_fx_rates_asof` (never per holding) — see
`compute_portfolio_performance`'s `_convert_amount`.

## Backfill (D2 amendment)

`app/scripts/backfill_portfolio_value_history.py` — first-enable only,
refuses a second run unless `--force`; start date is the earliest date any
currently-held auto-priced ticker has usable price history, capped at
`--years` (default 5) — never `holding.created_at` (a replace-import
deletes and recreates rows, so that column doesn't mean "date first
owned"). Never overwrites an existing row (`ON CONFLICT DO NOTHING`).
`app/scripts/backfill_benchmark_prices.py` is a plain ~5-year history seed,
no approximation, safe to re-run (idempotent upsert).

## Beat schedule

`capture-portfolio-value-snapshot-daily` / `capture-benchmark-index-prices-
daily`, both 20:30 ET Mon-Fri — after every market's close node (latest is
US `after_close` at 20:00 ET) and the 17:15 ET FX fetch.

## Explicitly out of Phase 1

Any frontend/chart; changes to `/portfolio/summary`/`compute_portfolio`;
intraday ranges; a true trade-ledger GIPS TWR; snapshot archival/
downsampling.
