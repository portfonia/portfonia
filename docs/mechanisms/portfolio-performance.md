# Portfolio Performance — Phase 1 + #366 correction + #377 holiday history + #383 catalog + #398 FX seed

Issue #360 (Phase 1) and issue #366 (tracking-start fix + composition-replay
removal, 2026-09-07 design amendment). Issue #377 (2026-09-08) keeps selected
benchmark history when the first real snapshot falls on a non-trading day
and separates display eligibility from comparison eligibility. Issue #382
is the Phase 2 UI-defaults follow-up: first visit is range `1M` and
benchmarks `[sp500]`, with multi-select over the catalog. Issue #383 shipped
`csi300` as an optional chip (PR #385). China A50 is **shelved** — not on
the near-term agenda; #383 is closed. Issue #398 seeds multi-year `fx_rates`
for every `fx_fetcher._PAIRS` entry so non-USD report currencies can draw
full selected-range benchmark lines (supersedes #365's not_planned close,
which assumed FX was only needed on portfolio-snapshot days).
Governing decisions: #360's Decisions comment + the 2026-09-06 amendment
comment + the Implementation design comment, #366's Design +
Implementation-contract comments, #377's five contract comments,
#382's five contract comments, #383's five contract comments, and #398's
five contract comments (read those before this file — this is an
implementation summary, not the spec itself). Paired Chinese-language
design doc: Obsidian `Hermes/Portfonia/Docs/Portfolio_Pfmc.md` §1.2–1.3,
§2 D9, §3.3.

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
performance` + one-off market-data seeds (`backfill_benchmark_prices.py`
for index closes; `backfill_fx_rates.py` for every `_PAIRS` FX pair,
issue #398). The per-user portfolio composition-replay backfill from
Phase 1 was retired by #366, see below. No frontend, no chart — Phase 2
is a separate follow-up PR against this frozen response contract.
Deliberately does not touch `/portfolio/summary` or `compute_portfolio`'s
`capture_supported=False` exclusion (D5 amendment: Performance computes
its own value rules independently — aligning the two is explicitly out of
scope for this phase, and #366 does not revisit this).

## Schema

- `portfolio_value_snapshots` — one row per holding per user per day.
  Denormalized, no FK to the live `holdings` row (a later edit/delete must
  not corrupt historical readability — same reasoning as `accounts`'
  broker/account/portfolio text columns). `holding_id` is a soft, nullable
  UUID (no FK) used for day-to-day quantity alignment in the TWR calc and
  for D8 current group/account attribution (issue #371).
  `user_id` is `ON DELETE CASCADE` — unlike holdings/reports/accounts
  (`RESTRICT`, issue #129 B7), this is derived time-series data, not an
  audited record, so a user purge needs no new step in
  `app/services/user_purge.py`.
- `portfolio_snapshot_batches` — per-(user, day) `pending|complete|
  skipped_deps` marker. The read API only ever considers `complete` days;
  `skipped_deps` means the day's FX dependency wasn't resolvable at write
  time; since issue #373 the day is retried by the bounded catch-up pass
  (previously the next run captured a *different* date, so a skipped day was
  effectively lost) rather than exposed half-computed. **This gate is FX-only,
  not FX-AND-price** (review
  5124107298 finding 3, PR #363) — deliberately: this codebase has no real
  market holiday calendar, so a symmetric "did today's price capture
  produce anything yet" check would misfire as `skipped_deps` on every
  market holiday for a single-market book. A per-holding price gap already
  degrades gracefully to `data_quality="insufficient"` on that one row
  instead of blocking the whole batch — see `stage_user_snapshot`'s
  docstring for the full reasoning.
- `portfolio_snapshot_outbox` — per-(user, day) capture outbox (issue #373):
  `computed|applied|failed` + the Fernet-encrypted intended payload and its
  sha256. See "Capture durability (issue #373)" below.
- `benchmark_prices` — daily close for `sp500|dow30|nasdaq|csi300` (Nasdaq
  Composite, not the Nasdaq-100 — D9; CSI 300 added in issue #383),
  unrelated to any user's holdings. Quote currency is stamped per row
  (`USD` for the three US indexes, `CNY` for `csi300`). China A50 is not
  a code — shelved, not on the near-term agenda; see "Catalog (issue #383)"
  below.

Migration: `c1d2e3f4a5b6_add_portfolio_performance_tables.py`.

## Capture durability (issue #373)

Since-tracking means a missed day cannot be honestly invented later, so the
daily capture was split into a freeze phase and a publish phase, with a
commit in between (`app/services/portfolio_history.py`):

1. `stage_user_snapshot` resolves holdings/prices/FX for one (user, day) and
   writes the intended rows into `portfolio_snapshot_outbox` as `computed`
   (Fernet-encrypted JSON payload + sha256; see
   `app/services/snapshot_outbox.py`). The batch row is created `pending` and
   is **not** `complete` yet.
2. `apply_outbox_row` decodes that payload, upserts it into
   `portfolio_value_snapshots`, flips the batch to `complete` and the outbox
   row to `applied` — all in one transaction. A `complete` batch therefore
   always has exactly the payload's rows behind it (contract constraint 4 of
   #373).

`capture_portfolio_value_snapshot` commits the freeze for the whole fan-out
before publishing any of it, so a failure during publishing leaves the day
recoverable by replay instead of lost.

**Transaction boundary: batch-level, not per-user (decision made while
implementing #373).** Design step 1 of the issue assumed one transaction per
`(user_id, snapshot_date)`; the pre-implementation note on the issue recorded
that the real commit boundary is one transaction for the whole fan-out. That
is what shipped, and it is what the Requirements allow: for a given
`(user_id, snapshot_date)` the rows and the batch status still commit
together (never a `complete` batch with missing rows, never rows without a
batch), and a failure leaves the day `pending`/`skipped_deps` — retryable and
now replayable. The accepted cost is blast radius: an exception anywhere in
the publish loop rolls back every user's rows for that day, and the retry is
a whole-batch run. Per-user boundaries would buy isolation this write path
has no per-user failure mode to need, at the cost of a partial-fan-out state
after any transient failure; the freeze phase is what makes the whole-day
rollback cheap to recover. Revisit if a per-user failure mode appears.

**Recovery (`app/services/snapshot_recovery.py`)**, exposed as
`POST /admin/portfolio/snapshots/recover` and as a bounded catch-up pass at
the end of the daily task:

- A day with an outbox row is **replayed** (from the payload, never recomputed
  from current holdings), whatever its age. `applied` rows whose live rows
  have gone missing are re-applied the same way.
- A day with **no** payload is recomputed from live holdings only when both
  (a) it is within `CATCHUP_LOOKBACK_DAYS` (7) and (b) the live book's
  composition fingerprint equals the newest frozen evidence before that day —
  i.e. the book provably has not moved since the last `complete` snapshot.
  A change-and-revert inside that window is undetectable without a holdings
  CDC (out of scope for #373); that residual is why the window is short.
- Anything else is skipped with a structured WARNING and left not-`complete`;
  `skipped_deps` days stay eligible for a later catch-up once FX lands.
- An undecodable payload is marked `failed` (logged, visible) and not retried.

**Retention**: `computed`/`failed` rows are never pruned (they are pending
recovery evidence). `applied` rows are redundant once the live rows exist —
that is the durable copy, with Postgres backups behind it — and are deleted
after `OUTBOX_APPLIED_RETENTION_DAYS` (90) by a bounded delete at the end of
the daily capture.

**Disk / whole-database loss** stays with the existing Postgres backup
practice (daily `pg_dump` to OCI Object Storage, plus the restore drills in
`docs/mechanisms/backup-and-ops.md`); #373 deliberately does not add a second
database product or a second copy of the outbox.

**Scenario 1 hardening evidence**: `test_mid_write_failure_never_leaves_a_partial_complete_batch`
(simulated exception mid-publish) plus the existing `db_session` rollback
semantics; the write path's own `session.commit()` sits between the freeze and
the publish. Note the intended side effect: a failed publish leaves `pending`
batch rows for that day, which `capture_health` already reports (#372), so the
failure is visible rather than silent.

Migrations: `9d2f4b7c1e05_add_portfolio_snapshot_outbox.py`. Related:
#367 (write path), #372 (detection, not recovery), vault
`Docs/Portfolio_Data_Retention.md` §2.4.

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
`holding_id` currently passes the active filter. A market/broker change
that drops the row from the current view is D8 snapshot-time outflow (not
a price move — the stored day-*t* row is still the right mark). A
group/account regroup does **not** drop the holding: issue #371 current
attribution follows the live (or last-snapshot) label for the whole
tracking history.

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
AND'd across dimensions (issue #371 layered attribution; vault
`Portfolio_Pfmc.md` D8 is SoT):

- **`groups` / `accounts` — current attribution.** Membership is the live
  `holdings` row's `portfolio` / `account` for that soft `holding_id`. After
  a regroup/reaccount, the entire since-tracking history of that id
  participates under the **new** label. Historical snapshot denorm for
  these two fields is **not** rewritten and is **not** the match key.
  Cleared/deleted holdings (no live row) fall back to that `holding_id`'s
  latest non-backfilled snapshot tags. Rows with `holding_id` null
  (legacy/anomaly) fall back to the row's own denorm so they are not
  silently dropped.
- **`markets` / `brokers` — snapshot-day facts.** Match each day's
  denormalized `market` / `broker`. A venue or custodian change is
  point-in-time; a sold lot still appears on the days it was held.

`portfolio.empty=true` only when literally no snapshot row in the selected
range matches the filter at all; an empty *current* book with matching
history still draws that history (cleared holdings via last-snapshot
group/account fallback). `is_backfilled=True` rows never match any filter
regardless of dimension selection (issue #366). Phase 2 filter option
lists seed from the current book's live holdings (`GET /portfolio/summary`
rows), not orphaned historical names.

## Currency

Each row's `market_value_base` is in whatever currency was live for that
user **at capture time** — recorded per-row in `base_currency` (issue #367
review finding A, added after Phase 1 shipped without it; see below). If
the request's `base_currency` differs from a given DAY's own recorded
currency, that day's already-aggregated total is re-converted via
`historical_fx_rates_asof` (never per holding) — see
`compute_portfolio_performance`'s `_convert_amount` and
`_build_portfolio_series`'s `_day_currency`. **Accepted approximation,
documented per blacktomb42's review follow-up (issuecomment-5556912227)**:
this re-conversion applies ONLY when the request's `base_currency` differs
from that day's own recorded one — the common case (frontend requests the
user's own current `base_currency`, matching what most days were captured
under) never hits this path at all. When it does apply, the conversion uses
the SAME 10-day-lookback historical FX rate the write path uses, at the
AGGREGATE level (one rate per day, not one per holding) — a day where the
two currencies' relative FX moved intraday, or where the lookback resolves
a slightly different date than the capture-time write did, is a real but
small source of imprecision, accepted for Phase 1 rather than re-pricing
every holding on every read.

**`portfolio_value_snapshots.base_currency` (issue #367 review finding A,
blacktomb42, review 5563537095)**: Phase 1 shipped WITHOUT this column —
the reader called `report_currency_for(user_id)` once per REQUEST and
treated every historical row as if it had always been denominated in
whatever the user's CURRENT preference is. `PATCH /me/report-currency`
(issue #350) lets a user change that preference at any time, so a row
written under an old preference and a row written after a change are
numerically incomparable without knowing what each one actually used — the
reader had silently assumed they were always the same unit. Reviewer's
repro: 100 USD cash, USD/CNY held constant at 7, preference USD on day 1
then CNY on day 2 — `market_value_base` goes 100 -> 700 with ZERO real
economic change (100 USD literally IS 700 CNY at that rate), but the old
reader read the jump as +600% TWR. Migration `d2e3f4a5b6c7` adds the
column (backfilled from each row's user's CURRENT `base_currency` — the
best available approximation for pre-existing rows, since no historical
preference-change log exists and, per the review, no production row so far
has actually hit this defect). `stage_user_snapshot` now records it
per-day; `_day_currency` in `portfolio_performance.py` reads it back **per
day** (never once per request) for both the raw-MV `_convert_amount` call
and the TWR chain, converting `v_prev` into DAY T's own currency before
computing `r_t = v_minus/v_prev - 1` so a preference change between two
adjacent days can't corrupt the ratio's unit consistency (`_contribution`'s
fast path was ALREADY safe — it derives a per-share price from `curr_row.
market_value_base` and never touches `prev_row`'s stored value directly —
the bug was entirely in how the aggregate day-values were compared, not in
that fast path).

**Report-currency change audit (issue #372 slice A)**: every successful
preference change (`PATCH /me/report-currency` or ops
`POST /admin/users/by-email/report-currency`) appends one
`report_currency_changes` row (`user_id`, `old_currency`, `new_currency`,
`changed_at`, `source` `self|admin`, `actor_user_id`). Self-service sets
`actor_user_id` to the caller. Ops-token writes have no JWT principal —
`actor_user_id` is `Settings.DEV_USER_ID` when that users row exists
(same stand-in as ticker-leverage `created_by`), else null. A no-op
same-currency write does not insert. The writer never rewrites historical
`portfolio_value_snapshots.base_currency`. Ops read:
`GET /admin/users/{user_id}/report-currency-audit` (newest first) or

```sql
SELECT old_currency, new_currency, changed_at, source, actor_user_id
FROM report_currency_changes
WHERE user_id = '<uuid>'
ORDER BY changed_at;
```

`user_id` is `ON DELETE CASCADE` (purge is not blocked; same class as
snapshots). Capture health is slice B in the same issue (see Beat
schedule below). Sector denorm is deferred (no product ask).

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
are deleted. `backfill_benchmark_prices.py` and `backfill_fx_rates.py`
(issue #398) are unaffected — index closes and FX daily rates are market
data, not a claim about the user's holdings, and can be backfilled freely.
Do not revive composition-replay / fake portfolio history (#366/#367).

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

## Common compare window (issue #366 D7, amended by #377)

Comparing a portfolio's cumulative % against a benchmark's is only
meaningful when both are measured over the **same** window. #366 correctly
banned unequal-window *comparison*, but the first implementation clipped
benchmark `points` to `[portfolio.start, portfolio.end]` and cleared the
series when no source date fell inside that window. That conflicted with
§1.4: selected indexes may keep their available history across the
requested range even when the portfolio line is a single real snapshot.

**#377 rule:** keep display history; separate `displayable` from
`comparable`. If the first displayed portfolio day `P0` has a usable
as-of index valuation `B(P0)`, the entire displayed index history is
`R(d) = B(d) / B(P0) - 1` (including `d < P0`). If `P0` cannot be valued,
the index still draws from its own first usable day (`normalization=
own_start`) and is marked not comparable. Ratios are computed from raw
Decimal valuations, never from already-rounded cumulative percentages.

`compare_start` is still the portfolio series' own `start_date` when the
series is non-empty. Comparison bounds never extend past `P1`. Points
after `P1` remain as market context and do not enter
`comparison_return_pct`. Unequal observed extents that would require
moving the portfolio/header anchor are still #368's problem — this path
uses the conservative `incomplete_window` / `anchor_unavailable`
classification instead of rebasing the portfolio.

When the portfolio series is `empty`, benchmarks self-normalize over the
requested range (`comparison_status=no_portfolio`, `comparable=false`)
and still draw.

## Bounded as-of index/FX valuation (issue #377)

`GET /portfolio/performance` bulk-loads index closes and required FX from
`range_start - 10` calendar days through `range_end`, plus one predecessor
row per index/pair to distinguish missing from stale. Evaluation for
calendar day `d`:

- latest `source_date <= d` with `d - source_date <= 10` (inclusive) and
  `close > 0`
- FX via the existing USD-pivot `to_base` formula; every pair used must
  itself be `<= d`, `<= 10` days old, and `> 0`
- `B(d) = close(s) * FX(d)`; an unchanged close can still move in the
  display currency when FX moves
- never future prices, request-time FX, interpolation, zero-fill, or
  unbounded carry-forward
- `price_as_of`, all `fx_as_of` pair dates, `carried`, and
  `unavailable_reason` are returned; `carried` means a source predates
  `d`, not that the market was closed
- interior/trailing gaps stay null; leading unavailable days are trimmed;
  GET does not fetch market data or write rows

Query count is independent of the number of calendar days (two index
queries + two FX queries when conversion is needed, plus the existing
portfolio reads).

**Review round fixes (issue #367, blacktomb42, review 5563537095) — both
required changes before merge:**

- **Finding 1 — a newly tracked sub-account inherited an unrelated
  account's pre-tracking start.** `_build_portfolio_series` looped over
  EVERY complete-batch date in the requested range, and `_day_value([])`'s
  "empty filtered set = legitimate $0" rule (D8's sold-lot case) applied
  indiscriminately to dates BEFORE a filtered dimension's own first real
  appearance too — a newly added `AccountY` that first exists on day 2
  still got a fabricated $0 point on day 1 (when only `AccountX` existed),
  making `portfolio_series.start_date` land on day 1 and, via the
  common-window logic above, giving every benchmark the wrong anchor.
  Fixed by computing `matched_dates` (dates where the active filter
  matches at least one row) and restricting the series to dates `>=
  matched_dates[0]` — a $0 point is now only ever produced ON OR AFTER a
  dimension's own first real match (sold-lot or market/broker drop;
  group/account regroup uses the #371 current-attribution predicate, so
  earlier tracking days of a retagged holding still count as matches). A
  date strictly BEFORE first appearance is excluded from the series
  entirely, not zero-valued.
- **Finding 2 — `comparable=True` didn't require actual overlap with the
  portfolio's real history.** The #366 clip originally capped its window at
  the raw REQUESTED range end, not the portfolio's own real last day — a
  benchmark point that existed only AFTER the portfolio's tracked history
  stopped fell inside `[compare_start, range_end]` and came back
  `comparable=True` with zero portfolio data to compare it against. #377
  keeps those later points as display context and sets
  `comparison_end = P1` with `comparison_return_pct` measured at P1, or
  `anchor_unavailable` / `incomplete_window` when P0/P1 cannot be valued.

## Production cleanup (one-off, issue #366)

The ~35,605 rows Phase 1's retired backfill wrote in production must be
deleted by ops — **not** superseded by a user-facing "correct my history"
product (explicitly out of scope; the read-path exclusion above is a
legacy-safety net, not a substitute for actually removing known-bad data).
See the PR description for the exact one-shot SQL/script, dry-run output,
and row counts actually deleted.

## Backfill (benchmark indexes + FX rates)

`app/scripts/backfill_benchmark_prices.py` is a plain ~5-year history seed
for catalog index closes, no approximation, safe to re-run (idempotent
upsert). Uses yfinance's `Ny` period form (`f"{years}y"`), NOT an
arbitrary `Nd` day count (review 5124107298 finding 2, PR #363) —
empirically verified against the installed yfinance 1.3.0 that `Nd` for a
large N does not error or return empty (it returns N trading-day ROWS,
which for large N spans MORE calendar time than N days: `1825d` returned
1825 rows spanning ~7.25 calendar years, not 5), so the original code was
not actually broken, but that row-count-not-calendar-days semantic is
surprising and unrelated to what `--years` means — `Ny` is the correct,
unambiguous form for this path. The short daily catch-up window keeps
`Nd` (e.g. `7d`), matching existing precedent elsewhere in this codebase
(`_yfinance.fetch_ohlcv_range`). After a catalog expansion, ops re-runs
this script so new codes (`csi300` as of #383) get the same multi-year
span; daily Beat then keeps them current. All catalog indexes are **price
indexes** (no dividend reinvestment), not total-return variants.

`app/scripts/backfill_fx_rates.py` (issue #398) is the FX sibling: same
`Ny` period, default `--years 5`, idempotent upsert on
`(pair, rate_date)`. It seeds **every** pair in `fx_fetcher._PAIRS`
(USDCNY, USDHKD, USDCNH, USDGBP, USDEUR, USDJPY, USDSGD, USDAUD, USDCAD,
USDCHF, USDKRW, USDTWD, USDMOP, USDNZD) — not a CNY/HKD-only subset.
Historical FX is observed market data: do **not** add `is_backfilled`.
Do **not** loosen `benchmark_valuation.LOOKBACK_DAYS` (10 calendar days)
or invent rates on gaps; missing/stale days stay `missing_fx` /
`stale_fx`. Daily `capture_fx_task` / `update_fx_rates` remains the
ongoing freshness path (today-only upsert). #365's not_planned close
assumed Performance only needed FX on real portfolio-snapshot days;
after #377, displayable index history spans the **selected range**, so
non-USD `base_currency` needs FX depth aligned with the benchmark seed.

**Ops (one-off after merge, production):** run
`python -m app.scripts.backfill_fx_rates` (default 5 years; `--years`
override allowed) once against the production database, then confirm
`min(rate_date)` / `count(*)` per pair and smoke
`GET /portfolio/performance?range=1Y&benchmarks=sp500` for USD vs CNY vs
HKD (plus one other `_PAIRS` currency, e.g. EUR). Comparable benchmark
`display_start_date` / `display_end_date` — not truncated to FX-capture
depth. Remote paths and host identifiers stay in the private ops vault,
not this file.

**Issues #402/#403 (2026-09-09) — both one-off seed scripts hit the #194
param-limit bug**: `backfill_fx_rates.py`'s bulk `fx_fetcher._upsert_fx_history`
(issue #402, PR #404 — reproduced live in production: ~5y x 14 pairs x 5
columns ~= 87,850 bound params, over PostgreSQL's 65535-per-statement cap)
and `backfill_benchmark_prices.py`'s bulk `benchmark_prices._upsert` (issue
#403, PR #405 — found by inspection, not yet a production incident) each
built one unbounded `INSERT` — the same bug class `price_capture.py`'s
`_upsert` was fixed for in issue #194 (see `capture-and-reporting.md`'s
OHLCV upsert entry for #194's original fix). `_upsert_fx_history` now
chunks at 5000 rows/batch
(`_UPSERT_BATCH_SIZE`); `benchmark_prices._upsert` chunks at 2000
rows/batch (`_UPSERT_CHUNK_SIZE`, matching #194's original margin for its
narrower 4-column rows). Both PRs landed independently within hours of
each other (#404 first; #405 was rebased on top and dropped its own
now-redundant copy of the `fx_fetcher.py` fix). Full incident detail and
the "why not a shared chunked-upsert helper" call: `capture-and-reporting.md`'s
"Same 65535-param bug recurred twice more" entry.

**Issues #406/#407 (2026-09-09) — two yfinance data-source gaps found while
verifying #402/#403's fix in production, different outcomes**: running the
now-fixed `backfill_fx_rates`/`backfill_benchmark_prices` at real volume
surfaced two separate vendor-data problems, not code bugs.

- **#406, closed**: `USDCNH` yfinance history is shallow; the one-off
  Twelve Data fill is issue #411. No Tencent/Sina historical fallback.
- **#407**: `csi300` (`000300.SS`) weekday holes after the Yahoo fetch
  are filled from Tencent kline (`sh000300`) when a same-chain Yahoo
  bar adjacent to the hole agrees
  (`abs(x-y) <= max(0.01, abs(y)*0.0001)`). No anchor or a disagreement
  writes nothing. Wired into `capture_benchmark_index_prices` and
  `backfill_benchmark_prices`. sp500/dow30/nasdaq unchanged.

## Beat schedule

`capture-portfolio-value-snapshot-daily` / `capture-benchmark-index-prices-
daily`, both 20:30 ET Mon-Fri — after every market's close node (latest is
US `after_close` at 20:00 ET) and the 17:15 ET FX fetch. Confirmed against `app/tasks/__init__.py`'s `_MARKET_NODES`/beat-schedule
ordering: no other daily entry in that file fires between 17:15 ET and
20:30 ET on a weekday, so both new tasks always run strictly after that
day's price-capture and FX-fetch tasks have had their scheduled chance to
run (not a guarantee they *succeeded* — since issue #373 the snapshot task
finishes with a bounded catch-up pass that replays frozen-but-unpublished
days and retries recently missed ones whose book provably hasn't moved; see
"Capture durability (issue #373)").

**Capture health (issue #372 slice B)**: `check-capture-health-daily` at
21:30 ET Mon–Fri. One probe after that window, not checks sprinkled into
every capture function. **Alert rule:** expected date = that ET weekday;
a pipeline is stale if it has no success evidence dated on that day
(`price_snapshots` close `trade_date` — any listed or fund close bar
clears the whole price pipeline, intentional v1 coarseness;
`fx_rates.rate_date`, `portfolio_snapshot_batches` `status=complete`
`snapshot_date`, `benchmark_prices.price_date`). Portfolio also alerts when that date has
`skipped_deps` or `pending` rows. One aggregated `send_ops_alert` +
`alert_dedup`, production-gated. Hard-fail retries stay on
`capture_tasks._capture_failed`. Not a 36h wall-clock rule (Monday vs
Friday would false-positive). No admin UI. Does not write or replay
(#373). Silence: `APP_ENV != production`, or a later weekday success
(new dedup key). Verify: `GET` is not provided; read the ops email /
`capture_health:` INFO log.

**Full-exit fan-out (issue #367 review finding B, blacktomb42, review
5563537095)**: `capture_portfolio_value_snapshot`'s user selection
originally was `User.id.in_(select(Holding.user_id).distinct())` only —
active users currently holding at least one position. `stage_user_snapshot`
already handles zero holdings correctly (marks the batch `complete` with
zero rows, D5's genuine $0 case), but that code path only runs if this
function calls it at all: a user whose LAST holding was deleted dropped out
of the `holdings`-backed selection permanently, so the exit day's
zero-holdings batch never got written, and the portfolio read back as
frozen at its last real value instead of reflecting the exit. Fixed by
OR'ing in every user who already has ANY `portfolio_snapshot_batches` row
(i.e. was ever tracked before) — once tracking starts, the daily fan-out
keeps checking that user for good, matching `tracking_start`'s own
"evidence, not current state" philosophy above.

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

## UI defaults (issue #382)

Phase 2 first paint originally used range `1Y` and all three US indexes.
`/portfolio/performance` now starts at range `1M` and benchmarks
`[sp500]`. The user can independently toggle `dow30` / `nasdaq` / `csi300`
(issue #383). Clearing every chip still draws the portfolio series;
comparable % copy only applies to selected comparable indexes. The GET
handler already treats omitted/empty `benchmarks` as zero series (not the
full catalog). The client always passes the chip list; an empty list
appends no `benchmarks` keys (same as omit → none). Backend `range`
default remains `1Y` when the param is omitted — the UI always sends
`range` explicitly. No #368 co-anchor, no TWR / `tracking_start` / D8
changes. Optional localStorage of last range/benchmarks is not
implemented.

## Catalog (issue #383)

Locked in this change:

| `index_code` | Vendor ticker | Currency | Semantics |
|---|---|---|---|
| `csi300` | yfinance `000300.SS` (SSE listing of CSI 300, `quoteType=INDEX`) | CNY | Price index |

Do not use `399300.SZ` (same index name; yfinance returns only the latest
session). Capture and backfill stamp `currency` on every upsert rather
than inheriting the column's USD server_default. The read path converts
via `fx_rates` and #377 as-of rules (10-day bound unchanged). Multi-year
FX history is seeded by `backfill_fx_rates.py` (#398; #365's "no
multi-year FX" note is reversed under this motive).

**China A50 shelved (2026-09-09).** Not on the near-term agenda. #383 is
closed after PR #385 shipped `csi300` only. Same-path yfinance research
(2026-09-08) is why it did not ship in that PR: `XIN9.FGI` is named FTSE
China A50 Index / CNY / INDEX but has no multi-year history (live quote
only); `XIN9.L` is delisted; `^XIN9` 404; `2823.HK` is an ETF proxy in
HKD; `000016.SS` is SSE 50, a different index. Do not invent a ticker.
Do not reopen A50 as leftover work on #383.

## Explicitly out of Phase 1

Any frontend/chart; changes to `/portfolio/summary`/`compute_portfolio`;
intraday ranges; a true trade-ledger GIPS TWR; snapshot archival/
downsampling.
