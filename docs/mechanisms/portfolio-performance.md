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
  half-computed. **This gate is FX-only, not FX-AND-price** (review
  5124107298 finding 3, PR #363) — deliberately: this codebase has no real
  market holiday calendar, so a symmetric "did today's price capture
  produce anything yet" check would misfire as `skipped_deps` on every
  market holiday for a single-market book. A per-holding price gap already
  degrades gracefully to `data_quality="insufficient"` on that one row
  instead of blocking the whole batch — see `write_user_snapshot`'s
  docstring for the full reasoning.
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
local-value. This fast path applies whether or not today's row for that
`holding_id` currently passes the active filter — a holding relabeled out
of the current sub-portfolio view (D8) still has its own stored day-*t*
row, and using it is what makes a relabel read as an outflow rather than a
price move.

**Full exit / row deleted entirely** (review 5124107298 finding 1, PR
#363): when a holding has NO snapshot row at all on day *t* — a full exit,
or the holding row itself deleted/replaced — this reprices the position
directly from `price_snapshots`/`fx_rates` as of day *t*
(`_reprice_from_source`), using the same `historical_price`/
`historical_fx_rates_asof` helpers `portfolio_history.py` uses to write
rows in the first place. An earlier version of this module simply excluded
such a holding from that day's numerator while the denominator still
included it — that turned a solo full exit into an approximately −100%
"return" instead of the cash-flow-neutral price move D3 requires, caught
by review before merge. The same reprice path also fires for a day-*t* row
that DOES exist but carries `shares == 0` (approval re-review leftover) — a
degenerate zero-share row has no usable per-share price to derive a mark
from, the same unpriceable situation as no row at all, and must not simply
fall through to exclusion either (that would silently reproduce the exact
−100% bug for this one row shape). Only when the position genuinely can't
be repriced (no `price_snapshots` row within the 10-day lookback either)
does it fall back to exclusion, matching D5's "insufficient" contribution —
that is now the ONLY reason a holding drops out of $V_t^-$.

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
`compute_portfolio_performance`'s `_convert_amount`. **Accepted
approximation, documented per blacktomb42's review follow-up
(issuecomment-5556912227)**: this re-conversion applies ONLY when the
request's `base_currency` differs from the stored canonical one — the
common case (frontend requests the user's own `base_currency`, matching
what was captured) never hits this path at all. When it does apply, the
conversion uses the SAME 10-day-lookback historical FX rate the write path
uses, at the AGGREGATE level (one rate per day, not one per holding) — a
day where the two currencies' relative FX moved intraday, or where the
lookback resolves a slightly different date than the canonical write did,
is a real but small source of imprecision, accepted for Phase 1 rather than
re-pricing every holding on every read.

## Backfill (D2 amendment)

`app/scripts/backfill_portfolio_value_history.py` — first-enable only,
refuses a second run unless real (non-backfill) history already exists;
**does NOT refuse a second run over only-prior-backfill history without
`--force`** (review 5124107298 finding 4 raised this as a possible gap —
confirmed intentional after re-checking the amendment's literal text,
"refuse if REAL non-backfill history already exists": a user with only
backfill rows is still "first enable", and `ON CONFLICT DO NOTHING` already
makes that re-run a safe idempotent no-op, matching the amendment's rule
that a rerun must never overwrite history already written to the database
directly). Start date is the earliest date any currently-held auto-priced
ticker has usable price history, capped at `--years` (default 5) — never
`holding.created_at` (a replace-import deletes and recreates rows, so that
column doesn't mean "date first owned"). Never overwrites an existing row
(`ON CONFLICT DO NOTHING`).

`app/scripts/backfill_benchmark_prices.py` is a plain ~5-year history seed,
no approximation, safe to re-run (idempotent upsert). Uses yfinance's `Ny`
period form (`f"{years}y"`), NOT an arbitrary `Nd` day count (review
5124107298 finding 2, PR #363) — empirically verified against the
installed yfinance 1.3.0 that `Nd` for a large N does not error or return
empty (it returns N trading-day ROWS, which for large N spans MORE
calendar time than N days: `1825d` returned 1825 rows spanning ~7.25
calendar years, not 5), so the original code was not actually broken, but
that row-count-not-calendar-days semantic is surprising and unrelated to
what `--years` means — `Ny` is the correct, unambiguous form for this
path. The short daily catch-up window keeps `Nd` (e.g. `7d`), matching
existing precedent elsewhere in this codebase
(`_yfinance.fetch_ohlcv_range`).

## Beat schedule

`capture-portfolio-value-snapshot-daily` / `capture-benchmark-index-prices-
daily`, both 20:30 ET Mon-Fri — after every market's close node (latest is
US `after_close` at 20:00 ET) and the 17:15 ET FX fetch. Confirmed against `app/tasks/__init__.py`'s `_MARKET_NODES`/beat-schedule
ordering: no other daily entry in that file fires between 17:15 ET and
20:30 ET on a weekday, so both new tasks always run strictly after that
day's price-capture and FX-fetch tasks have had their scheduled chance to
run (not a guarantee they *succeeded* — that's what `skipped_deps` and the
daily task's own idempotent re-run cover).

## Other accepted Phase 1 tradeoffs (document only, per blacktomb42's
review follow-up issuecomment-5556912227)

- **`market_value`/`market_value_base` are plain `Numeric`, not
  encrypted**, while `shares`/`current_value` on the same table are
  `EncryptedDecimal` — intentional for Phase 1 (these two columns are
  needed for the TWR read path's arithmetic and are less individually
  identifying than a raw share count or a manually-typed dollar value), but
  logged here as encryption-scope debt to revisit if that judgment call
  changes.
- **Header mixes a dollar market-value change with a TWR percentage** when
  `twr=true` (`PerformanceHeader.value_change_base` is always the raw $
  change, `value_change_pct` is the TWR-chained % when `twr=true`) — this
  is the Phase 1 API contract as specified (the issue's requirement 7:
  with TWR on, the header dollar figure is labeled "market value change",
  never "return"); Phase 2's UI copy needs to make the distinction between
  the $ figure and the % figure clear to the user, not something this API
  response can resolve on its own.

## Explicitly out of Phase 1

Any frontend/chart; changes to `/portfolio/summary`/`compute_portfolio`;
intraday ranges; a true trade-ledger GIPS TWR; snapshot archival/
downsampling.
