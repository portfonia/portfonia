# Portfolio Performance — Phase 1 (backend only) + issue #366 correction

Issue #360 (Phase 1) and issue #366 (tracking-start fix + composition-replay
removal, 2026-09-07 design amendment). Governing decisions: #360's Decisions
comment + the 2026-09-06 amendment comment + the Implementation design
comment, and #366's Design + Implementation-contract comments (read those
before this file — this is an implementation summary, not the spec itself).
Paired Chinese-language design doc: Obsidian
`Hermes/Portfonia/Docs/Portfolio_Pfmc.md` §2 (D2/D5/D7) + §6.

**#366 in one line**: Phase 1's one-off portfolio backfill derived a
position's chart start date from "earliest ticker price we happen to have"
(`price_snapshots`), not from when the user was actually being tracked —
production verification found ~35,605 fictional rows across 4 accounts with
start dates in 2024-11/12 for a product that only existed since mid-2026.
The backfill script is deleted outright (not deprecated); `GET /portfolio/
performance` now derives `tracking_start` from the first real (non-
backfilled) complete snapshot day, and co-normalizes cumulative % against
benchmarks to a common compare window instead of each series' own
independent start. See "Since-tracking start" and "Common compare window"
below.

## Scope

Phase 1 ships schema + two daily Celery tasks + `GET /portfolio/
performance` + one one-off backfill script (`backfill_benchmark_prices.py`
— benchmark index history only; the per-user portfolio composition-replay
backfill from Phase 1 was retired by #366, see below). No frontend, no
chart — Phase 2 is a separate follow-up PR against this frozen response
contract. Deliberately does not touch `/portfolio/summary` or
`compute_portfolio`'s `capture_supported=False` exclusion (D5 amendment:
Performance computes its own value rules independently — aligning the two
is explicitly out of scope for this phase, and #366 does not revisit this).

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
book with matching history still draws that history. `is_backfilled=True`
rows never match any filter regardless of dimension selection (issue #366).

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

## Since-tracking start, not composition-replay (issue #366, supersedes D2)

Phase 1's `backfill_portfolio_value_history.py` picked a position's chart
start date from "the earliest date `price_snapshots` happens to have usable
price history for this ticker," capped at `--years`. That conflates **price
data availability** (a technical-analysis lookback concern) with **when the
user's holdings were actually tracked** — this codebase stores quantity +
average cost, never a buy date or trade ledger, so replaying today's book
backward on market prices produces a curve for a portfolio composition that
may never have existed on those dates. Production verification exposed this
directly (~35,605 rows, starts in 2024-11/12, for a product live since
2026-05) — full incident writeup in vault §6.

**Retired outright, not deprecated behind a flag**: the script, its
dedicated test file, and every doc/comment instructing it as a product step
are deleted. `backfill_benchmark_prices.py` is unaffected — index closes are
market data, not a claim about the user's holdings, and can be backfilled
freely.

**`tracking_start`** (`GET /portfolio/performance`'s `portfolio.
tracking_start`) is now the evidence-based replacement: the earliest
`snapshot_date` with a `complete` batch and at least one row with
`is_backfilled=False`, for that user, **unfiltered** by market/group/broker/
account (`_tracking_start` in `portfolio_performance.py`). No new column —
the first real snapshot day already IS the day tracking began; a signal
inventory in vault §6.4 checked `User.created_at` (registration ≠ tracking),
a durable "first confirm" flag (never existed), `upload_jobs`' earliest row
(purged ~30 days later), and `accounts.MIN(created_at)` (a weak proxy,
missing entirely for a broker-less holding) before settling on this.

**`is_backfilled`/`approx_backfill` schema fields remain** for legacy
safety during the transition (a production purge should remove all such
rows outright — see "Production cleanup" below — but the read path does not
depend on that): `Filters.matches` unconditionally excludes any
`is_backfilled=True` row from every aggregate, TWR link, and the
`any_match_in_range` empty-check; `_build_portfolio_series` additionally
drops any date whose snapshot rows are ALL backfilled from the series
entirely (not a $0 point — that reading is reserved for a real zero-holdings
day or a dimension filter matching nothing that day, D8's "sold lot" case;
a day with only known-bad rows has no real tracked data at all).

**New holdings never trigger a backfill, still** (D7, unchanged by #366):
a holding's row enters the series starting the first day it has a real
snapshot, full stop — no retroactive rewrite of already-drawn history. The
dilemma this forecloses (vault §6.3): re-backfilling the WHOLE book every
time a holding is added would rewrite history nightly and still only answer
"what if today's names had always been held," never the user's actual past
performance — rejected regardless of engineering cost, because quantity +
average cost cannot uniquely reconstruct a multi-year trade path even in
principle (industry precedent survey: vault §6.5, Sharesight/Wealthfolio/
Fidelity).

**Summary's unrealized % and Performance's since-tracking % are deliberately
different questions**, not two measurements of the same thing to reconcile
(vault §6.3's worked example: cost 50 -> first tracked price 100 -> later
price 110 is simultaneously "+120% vs cost" and "+10% since tracking," both
correct). No product path should fake-align them.

## Common compare window (issue #366 D7)

Comparing a portfolio's cumulative % against a benchmark's is only
meaningful when both are measured over the **same** window. Phase 1
normalized the portfolio and each benchmark independently, each to 0% at
its own first available point over the FULL requested range — correct when
the portfolio's own data already spans that whole range, wrong the moment
`tracking_start` (or a dimension filter — see below) makes the portfolio's
real first point land later than a long-lived benchmark's.

`compare_start = max(range_start, tracking_start, first_portfolio_point_in_
filtered_series)`. In this implementation the last term always dominates
the other two once the portfolio series is non-empty: `_complete_batch_
dates` already bounds every candidate day to `range_start`, and a real
(non-backfilled) row can never predate `tracking_start` by construction —
so `compare_start` reduces in code to simply `portfolio_series.start_date`
(`compute_portfolio_performance`). The three-term form in the design doc
documents WHY that point ends up where it does, not a separate computation.

`_rebase_benchmark_to_window` re-anchors each benchmark's already-computed,
independently-normalized series to `compare_start`: `new_pct(t) = (1 +
old_pct(t)) / (1 + old_pct(compare_start)) - 1`, algebraically exact since
`old_pct(t) = price(t)/price(base) - 1`. This clips `points` to `[compare_
start, range_end]` while preserving the benchmark's own true `start_date`
for disclosure — the API still reports where each series' real data begins,
it just never presents an unequal window as head-to-head outperformance. A
benchmark with zero points inside the compare window (e.g., its only data
predates `tracking_start` entirely) comes back `comparable=False` with an
empty `points` list rather than a silently cross-window percentage.

When the portfolio series is `empty` (no matching history in range at all),
benchmarks stay self-normalized over the full requested range — there is no
comparison target to co-normalize against, and the line must still draw
per the original Phase 1 requirement (§1.5).

## Production cleanup (one-off, issue #366)

The ~35,605 rows Phase 1's retired backfill wrote in production must be
deleted by ops — **not** superseded by a user-facing "correct my history"
product (explicitly out of scope; the read-path exclusion above is a
legacy-safety net, not a substitute for actually removing known-bad data).
See the PR description for the exact one-shot SQL/script, dry-run output,
and row counts actually deleted.

## Backfill (benchmark only)

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
