# Capture layer, incremental reporting, and shared-compute stages (ADR-002, Ring 1 A1-A4)

### FX currency coverage + ticker-normalization consistency (issue #204, PR #253)

**Trigger**: PSH (Pershing Square Holdings) silently excluded from §1/totals
across two separate report runs three days apart (#204, later #249 as a
duplicate). Three independent bugs, not the "missing price_snapshots row"
the issue title suggested — verified against real production data before
any fix:

1. **FX pair coverage.** `VALID_CURRENCIES` (`app/schemas/holdings.py`) has
   always listed 14 currencies, but `fx_fetcher._PAIRS` and
   `portfolio_calculator._CURRENCY_TO_FX_PAIR` only covered CNY/CNH/HKD —
   the other 11 (GBP/EUR/JPY/SGD/AUD/CAD/CHF/KRW/TWD/MOP/NZD) had no FX
   pair anywhere, so `_to_base()` always returned `None` for them regardless
   of price correctness. All 11 added, mirroring the existing pattern. Two
   drift-guard tests pin `_PAIRS`/`_CURRENCY_TO_FX_PAIR` each to
   `VALID_CURRENCIES`, plus a third (review finding, round 1) pinning them
   to each other directly — either one alone matching `VALID_CURRENCIES`
   does not guarantee the two tables' pair *names* agree with each other.
2. **Ticker collision.** Bare `PSH` resolves on yfinance to an unrelated
   US-listed ETF (~$50, exchange "BTS"), not the real LSE-listed Pershing
   Square Holdings (`PSH.L`). `price_snapshots` *did* have a row for
   `PSH`/`US` — it was pricing the wrong security. `_yfinance.py` gained
   `_TICKER_SYMBOL_OVERRIDE` (a hardcoded collision table, currently just
   `{"PSH": "PSH.L"}`) composed into a new `_normalize_ticker()` alongside
   the existing HK suffix normalizer (issue #64/#69) — same shape as #69's
   fix, not a new mechanism.
3. **GBX vs GBP.** yfinance quotes `PSH.L` in GBX (pence, a subunit of GBP),
   not GBP itself (`fast_info.currency == "GBp"`). Found while fixing #2:
   without correcting this, fix #1 alone would have valued the holding
   100x too high. `_yfinance.py` gained `_TICKER_PRICE_SCALE` (per-ticker
   multiplier, currently `{"PSH.L": 0.01}`), applied at every price
   extraction point (`fetch_last_close`, `fetch_ohlcv_range`, `fetch_spot`).

**The consistency invariant this surfaced** (review round 1/2, blacktomb42 —
2 further rounds of `CHANGES_REQUESTED` after the first fix landed): capture
writes `price_snapshots` rows keyed by `_normalize_ticker(raw)`, so **every**
downstream consumer that re-derives a lookup/join key from a raw
`Holding.ticker` must apply the exact same normalization, not just the
call sites the original bug happened to touch. This turned out to be more
call sites than #69's HK fix needed, because #204 landed after several new
consumers of the identifier convention had been added since:

- `price_capture.capture_prices` / `fetch_last_close` / `fetch_ohlcv_range`
  / `fetch_spot` (write side — `fetch_spot` previously had **no**
  normalization at all, a pre-existing gap independent of #204's PSH case)
- `portfolio_calculator.compute_portfolio` (§1 valuation)
- `window_data.select_user_anomalies` / `window_data.compute_global_moves`
  (via `user_scope.global_identifier_universe`, the identifier-universe
  producer — round 1 finding: this one was missed in the first PR revision,
  splitting PSH across two identifiers, correct in §1 but silently absent
  from anomaly detection and L1 facts)
- `report_assembly._identifier` (holdings-listing print key, must match the
  L1 block's key or the model can't connect prose to a listed holding)
- `ticker_intel._holding_identifier` (feeds `large_weight_identifiers`) and
  `ticker_intel.build_l1_facts`'s `technical_positions` join key
- `technical_position.compute_technical_position` (round 2 finding: this one
  queries `price_snapshots` directly with the raw ticker rather than joining
  against an already-normalized dict, so it needed the fix at the *query*
  itself, not just a join-key adjustment — otherwise §4.4 stays permanently
  empty for any ticker needing normalization, independent of every other
  fix in this list being correct)
- `price_fetcher.update_holding_prices` (round 2 finding: `fetch_last_close`
  returns normalized keys, but the write-back loop matched against the raw
  `Holding.ticker` — so `market_price`/`price_as_of` were never refreshed
  for PSH going forward, meaning `compute_portfolio`'s `h.market_price`
  fallback stayed pinned to the pre-fix, wrong-security value indefinitely
  whenever a fresh `price_snapshots` close wasn't available for some reason)
- `routers/holdings._tickers_with_sparse_history` (round 2 finding: the
  confirm-time check for "does this ticker already have enough close bars"
  queried by the raw ticker too, so it never found the normalized rows and
  re-enqueued a full 420-day `backfill_ohlcv_task` on every single confirm
  for a ticker that already had a year of correctly-captured history)

`holding_parser.py`'s own `_normalize_hk_ticker` (a *separate*, older
function — parse-time DB-write canonicalization, e.g. `02333.HK` →
`2333.HK`) is intentionally untouched: it operates at a different layer
(what gets stored on `Holding.ticker`) than `_yfinance._normalize_ticker`
(what gets used as a lookup/fetch key), and #204's bug was entirely in the
latter.

**Deferred, not part of this fix** (#252, product-owner scoping decision):
the capture scheduler (`_MARKET_NODES` in `app/tasks/__init__.py`) only
ever runs for `US`/`HK`/`A-Share` — `market="Other"`, the schema's own
catch-all for a non-US/HK/CN listing, has zero scheduled capture at all.
PSH only got priced (wrongly, pre-fix) because it happened to be declared
`market="US"`. Building a general foreign-listing capture mechanism —
timezone/session-node scheduling for an arbitrary exchange, a non-hardcoded
ticker-suffix resolution scheme, per-exchange currency-subunit handling —
is a design task, not a bug-fix extension of this one.

### Fund NAV realtime path: Sina Finance fallback (issue #20)

`fund_nav_fetcher.py`'s realtime path (`_fetch_nav` → `update_fund_navs`, keeps
`holding.market_price` current) reads Tiantian Fund's `fundgz.1234567.com.cn`
JSONP endpoint. As of 2026-08-10, direct `curl` from the OCI production host
confirmed this endpoint returns Eastmoney's app-layer block page (HTTP 200,
HTML "页面未找到") for every fund code — not a network-reachability gap, an
actual block, and the historical path (`fetch_nav_history`/lsjz, used by
`capture_fund_navs` for `price_snapshots`) is unaffected and confirmed
reachable from the same host. `_fetch_nav` now falls back to Sina Finance
(`hq.sinajs.cn/list=f_{code}`, GBK-encoded) when fundgz fails, via a new
`_sina_fund_nav` helper (two-attempt retry, increasing timeout) ported from
the sibling `portfolio-agent` project's `collector_v2.py::_sina_fund_nav`,
which hit and solved this same block on 2026-07-30 and cross-validated Sina's
numbers against Tencent's `qt.gtimg.cn/q=jj{code}` as a second source.

- **Scope**: only the realtime path. `fetch_nav_history`/`capture_fund_navs`
  (lsjz) needed no fallback — confirmed working, left unchanged.
- **Verification**: production `curl` (two fund codes, two Eastmoney block
  hits, one Sina success matching the expected `name,nav,nav,cum_nav,date,...`
  shape) before writing any code; TDD red→green in the worktree afterward.
  PR #134 review (blacktomb42, Approve, 0 bugs / 2 suggestions / 1 nit) added
  3 more red→green tests covering the fixes: fundgz's per-fund block-page log
  downgraded ERROR→WARNING (only the terminal both-sources-fail case logs
  ERROR now), Sina retry scoped to `httpx.HTTPError` only (a parsed-but-bad
  200 returns immediately, no wasted second round-trip), and the Sina quote
  regex anchored on `hq_str_f_{fund_code}=` instead of "any first quoted
  string". 10 tests total in `test_fund_nav_fetcher.py`, full backend suite
  green (499 passed).
- **Test-infra gotcha hit while adding the log-level tests**: see "Tests"
  section below (`caplog` + alembic `fileConfig`).
- **Separate finding, not fixed by this change, tracked as issue #135**:
  while investigating, 3 production holdings (fund_codes
  019547/110011/008142, `pricing_mode=auto`) were found with 0
  `price_snapshots` rows despite `lsjz` being reachable and
  `capture_fund_navs` working correctly when invoked manually in production
  (70 rows written on manual run) — the scheduled `capture-fund-navs-daily`
  beat task (`0 20 * * mon-fri`) had not populated them across at least 2
  scheduled windows. Root cause not established (celery-beat/worker had been
  recreated ~1h before diagnosis by an unrelated deploy, wiping the logs that
  would show why). As of this note (2026-08-13, ~19:35 CST) the 20:00 CST
  run that would be the next real test hadn't happened yet — check issue
  #135 for the outcome rather than assuming either way.


### Fund NAV staleness observability (issue #298, PR #303)

`capture_fund_navs` now emits per-fund signals the aggregate `written=N` INFO
log cannot express — the exact gap that blocked root-causing #135 twice (a
mixed run writes rows for every fund but different funds land at different
stale trade_dates, and the logs that would explain why keep getting wiped by
unrelated deploys):

- **Stale latest NAV**: freshest returned `nav_date` more than one A-share
  trading session behind today (CST) → per-fund WARNING + ops alert
  `ops-fund-nav-stale-{fund_code}-{nav_date}`. Sessions are approximated as
  weekdays (no China holiday table in the codebase — weekends handled
  correctly, long holiday weeks can over-count; swap in a real XSHG calendar
  if logs ever show false positives). Friday NAV on Monday before the
  Monday-evening publish is the expected 1-session lag and stays silent;
  Thursday NAV on Monday (the 513500 shape) alerts.
- **Missing NAV history**: `fetch_nav_history` returning `[]` for a fund
  (HTTP/parse miss, or no rows in the lookback window) → per-fund WARNING +
  ops alert `ops-fund-nav-empty-{fund_code}-{cst_date}`, re-surfacing daily
  while the miss persists.
- **Durable dedup** (`app/core/alert_dedup.py`, same swappable-backend shape
  as `idle_activity.py`): Resend's 24h Idempotency-Key only collapses
  same-task retries, which is not enough for a 24h-apart weekday beat — the
  Redis record (90-day TTL as a GC safety net; keys embed the state so a
  changed NAV date makes a fresh key) is what stops a stuck NAV date from
  re-alerting daily. Fail-open on Redis outage (deliberately opposite
  `rate_limit.py`'s fail-closed convention): losing the dedup is better than
  losing the alert.

Observability only: capture behavior and the `capture-fund-navs-daily` beat
(20:00 CST Mon-Fri) are untouched. The check lives inside
`capture_fund_navs`, so the confirm-time `backfill_fund_navs_task` path gets
the same signals. Cross-ref: issue #135 (same symptom, root cause still
unconfirmed — this is the instrumentation for it, not a fix).


### Capture layer + incremental reporting (ADR-002)

Full spec in Obsidian: `Hermes/Portfonia/Docs/Incremental Report & Capture Layer Design.md`.

A **capture layer** (global, credit-free — RSS + yfinance; persists `news` +
`price_snapshots`, 1yr) runs at market-session nodes and feeds a **report
layer** (per-user, incremental).

- **Capture nodes** via crontab `nowfun` per market: US in ET (DST-aware),
  HK/CN fixed-offset. Nodes: US pre_open/open/close/after_close; HK/CN
  open/close. News captured at every node; catch-up logic lives in the task
  (range fetch + idempotent upsert), no watermark table.
- **OHLCV upsert + confirm-time backfill (issue #194/#195, PR #197)**:
  `price_snapshots` is global (no `user_id`) — two users holding NVDA share
  one close series. `_upsert` writes in 2000-row chunks (close-node rows
  bind 10 params; PostgreSQL/psycopg cap is 65535 per query). Daily
  `capture_prices_task` stays full-universe, `lookback_days=7`. Confirm-time
  `backfill_ohlcv_task` takes **this user's** auto tickers with <50 close
  bars, passed through `capture_prices(..., tickers=)` — it must not rescan
  `_market_tickers()` as the fetch universe (that is what overflowed when a
  second user's eight new US names widened the system-wide set). The ops
  script `backfill_ohlcv.py` remains the one-shot full-universe seed.
  `create_bug_report` truncates bodies at GitHub's 65536-char limit via
  `truncate_text` in `github_issues.py`; `_capture_failed` also caps
  `str(exc)` (per-market entries sliced before join so one huge SQL dump
  cannot crowd later markets out of the alert).
- **Fund NAV confirm-time capture (issue #196)**: `fund_code` holdings
  (no ticker) are not on the OHLCV path, so they used to wait until the
  next `capture-fund-navs-daily` beat (20:00 CST Mon-Fri) — a new user's
  first report could drop every fund as "missing price data". Same shape
  as the ticker fix: storage and the daily task stay global;
  `confirm_holdings` dispatches `backfill_fund_navs_task.delay(codes)`
  fire-and-forget (confirm stays fast — do not fetch inline, issue #193)
  for **this user's** auto funds with *zero* close snapshots. Threshold is
  not the ticker `< 50 bars` rule: `compute_technical_positions` skips
  no-ticker holdings, so valuation only needs any cached close (including
  one another user already captured). Lookback is 30 days, matching the
  scheduled task, not the ticker path's 420. `capture_fund_navs` takes an
  optional `fund_codes` filter (`None` = full universe) and uniques by
  fund_code before fetch — two holdings of the same fund must not propose
  the same upsert key twice.
- **Report window** = `[previous report.period_end, now]`; watermark =
  `max(period_end)` over the user's completed reports (deleting a report
  rolls it back; regenerate keeps the stored period). News/anomalies are read
  from the stores via `window_data`, never live RSS or last-two-closes.
  News selection is NOT range-bounded by this watermark on the lower end —
  see "News dedup ledger" below (issue #30) for why.
- **Cadence (issue #191, per-user `users.report_cadence`, 2026-08-28)**:
  `_REPORT_CADENCES` (`app/tasks/__init__.py`) is a table of Beat rows, each
  naming a `cadence` that scopes its own `active_user_ids` fan-out — not a
  single fixed schedule applied to everyone. Two rows today: `mwf` fires
  Mon/Wed/Fri 17:00 ET (moved from 16:30 ET on 2026-06-19, widening the gap
  after the 16:05 ET FX capture and 16:00 ET close capture), requires the
  user to have at least one holding; `weekly` fires Saturday 19:00 ET
  (`session_node="weekend_snapshot"`, not `"after_close"` — no market
  actually closed at that trigger), does NOT require holdings (issue #221
  §8 empty-book content contract). No capture task runs on weekends
  (`_MARKET_NODES` and every other daily capture entry are Mon-Fri only), so
  a weekly report's holdings/price data is always Friday's snapshot
  regardless of the exact Saturday time — only the live, generation-time
  macro/news search (`ticker_intel.py`/`cross_name_intel.py`) can reflect
  anything that happened over the weekend. `celery_app.conf.timezone =
  "America/New_York"` means neither row needs a `_node_cron`/nowfun
  wrapper — that's only for the HK/CST market nodes below. Full design
  record: Obsidian `Hermes/Portfonia/Docs/Ring 1-B Cadence.md`.
- **Ops cadence changes**: `POST /admin/users/{user_id}/cadence`
  (`app/routers/admin.py`) changes a user's `report_cadence`, same
  auth/audit pattern as every other `/admin/*` endpoint. Validated against
  the same `{"mwf", "weekly"}` set as `users`' DB `CheckConstraint`
  (`VALID_REPORT_CADENCES`, `app/models/user.py`) — the two are kept in
  sync by hand, not derived from one source, since Pydantic's `Literal`
  needs compile-time members.
- **Multi-user fan-out (Ring 1 stage A1, issue #128, PR #151; cadence-scoped
  since issue #191)**: `generate_incremental_report` iterates
  `app.services.user_scope.active_user_ids(session, cadence)` — active
  `users` rows on that cadence, gated by holdings only for cadences in
  `_HOLDINGS_GATED_CADENCES` (`user_scope.py`) — instead of the old fixed
  `DEV_USER_ID` single call. Each user's `generate_report` call is isolated
  in its own try/except: one user's failure is ops-alerted and logged, does
  NOT stop or retry the rest of the batch — but if EVERY user in a batch
  fails, the
  task still escalates to the normal 3x/5-minute Celery retry rather than
  reporting a false "completed". `generate_report(user_id=..., moves_cache=...,
  now=...)` — all three optional, `None`/omitted preserves the old
  single-user behavior for any other call site (manual trigger, tests).
- **Anomaly detection is split into a global pass + a per-user pass**
  (issue #128 A1): `window_data.compute_global_moves` computes each
  identifier's window price move exactly ONCE per batch (shared across
  every user who holds it, via a `moves_cache` keyed on the exact
  `(period_start, period_end)` window — `generate_incremental_report`
  stamps one shared `now` for the whole batch so this cache key actually
  collides across users); `window_data.select_user_anomalies` then does the
  per-user threshold judgment (two users can classify the same identifier
  under different `asset_class`, so the same move can clear one user's
  threshold and not another's) + theme merge, in-memory, no DB/LLM calls.
  `detect_window_anomalies(session, start, end, user_id, moves_cache=None)`
  is the thin single-user wrapper — `user_id` is now a required argument;
  the pre-A1 signature queried ALL holdings with no user filter at all,
  which was a real cross-user data leak once more than one user exists.
  Fires on EITHER trigger: `single_day` (one trading day beyond per-class
  per-day threshold — catches a violent session a net move would smooth
  away) or `cumulative` (baseline→latest net move beyond a scaled, capped
  threshold). Flagged holdings carry a session arc (prev close/open-gap/
  intraday range/close/after-hours) for §4.2. Report always says "this
  report period", never "today".
- **Tavily fan-out fairness (issue #128 A1 finding, resolved by A2's
  `search_cache`)**: the shared Tavily daily search budget used to be
  consumed sequentially in fixed `active_user_ids()` UUID order within a
  fan-out batch — users processed later in a given day could inherit
  `daily_remaining=0` and get no search-augmented context. Root-cause fix
  was reuse/dedup, not reallocating a fixed-size budget — see the L1
  shared-intel entry below.
- Portfolio valuation reads the **latest captured close** from
  `price_snapshots`, falling back to `holding.market_price` only for funds
  (no ticker). FX anomalies are not computed (FX stays daily in `fx_rates`).
- **L1 shared ticker-intel cache (Ring 1 stage A2, issue #128, PR #155)**:
  a new analysis stage (not a refactor — before A2, per-identifier "what
  happened to this security" narrative only existed inside each user's own
  Pass 2 call). `ticker_intel.get_l1_intel_batch` computes one LLM analysis
  per `(identifier, trade_date, prompt_version)` and caches it in the
  `ticker_intel` table — two users sharing an identifier in the same
  fan-out batch pay for one LLM call, not one each. `search_cache`
  (`(query_hash, trade_date)`) does the same for Tavily queries, closing
  the A1 fan-out fairness gap above. Both get a 90-day cleanup beat task
  (`cache_tasks.py`).
  - **Hard type boundary, not a discipline, keeps per-user data out of the
    shared cache**: `l1_identifiers_for_user` is the only channel from a
    user's own (per-user-judged) anomaly list into L1 — its return type is
    `list[str]`, identifiers only. Every numeric fact then comes from
    `window_data.resolve_global_moves`/`HoldingMove`, which is global by
    construction. This shape exists because the first draft read numbers
    straight out of the per-user-weighted `PriceAnomaly` structure, and
    three independent review rounds each found a different per-user value
    (a threshold-derived `trigger` field, then value-weighted
    price/pct fields from theme-merged anomalies, then a theme-slug-keyed
    news lookup) that had leaked into the shared cache — auditing fields
    one at a time was losing that race. The same rule applies to any
    future cross-user shared-cache consumer (A3's `macro_event_intel`,
    A4): selection may be per-user, values must not be.
  - **L1 describes exactly one trading day, never a report window**: an
    earlier version scoped L1's price-move fact to the calling user's own
    `[period_start, period_end]` (`period_start = user_watermark(user)`,
    per-user by construction) — two users analyzing the same identifier on
    the same day could get different windows, and whichever `generate_report`
    call reached L1 first cached its own window's numbers for everyone else
    that day. Fixed by dropping the window concept entirely:
    `window_data.day_window_bounds(trade_date)` is a pure function of the
    date only.
  - **Compliance scan runs on stripped output, matching Pass 2**:
    `_generate` calls `_strip_markers` (same as Pass 2's
    `cleaned = _strip_markers(raw_body)`) before `_scan_forbidden_output` —
    without it, a model-emitted disclaimer line could false-trip the scan
    and permanently blacklist that identifier's cache slot for the day.
  - **A headline-only candidate (no captured close yet, e.g. a pre-market
    manual run) is skipped entirely, not cached**: caching it would lock
    that identifier's slot for the whole trading day — even after the real
    close is captured later (e.g. the scheduled `after_close` batch), a
    cache hit would keep serving the earlier, unsupported-by-data version.
  - `LOW_COST_LLM_MODEL` is used with `data_collection=deny` kept enforced
    (no BYOK exception — L1 identifiers are holdings-derived, unlike Pass
    1's public-only inputs) under the default provider pin
    (`OPENROUTER_PROVIDER_ORDER`) — confirmed via a real OpenRouter call
    with these exact parameters that this alias is actually served under
    deny, not just assumed.
  - Full 7-round review history and design rationale:
    `Docs/Ring 1-A design.md` §4.3/§4.8 (Obsidian).


### L2 shared macro-event cache (Ring 1 stage A3, issue #128)

The second shared-analysis layer, same shape as A2's L1 but keyed on EVENTS
instead of identifiers: `macro_event_intel.get_l2_intel_batch` computes one
LLM inference per `(event_key, trade_date, prompt_version)` — "what is this
event, and which asset classes/sectors does it bear on" — and caches it in
the `macro_event_intel` table, so a macro theme or scheduled release that
shows up in three users' reports is reasoned about once. Same 90-day beat
cleanup as A2 (`cache_tasks.py`, extended rather than duplicated). Like A2,
**A3 does not change report content**: `ctx.macro_event_intel` /
`ctx.macro_event_exposure` are stored on `report_inputs` but never fed to
Pass 2 — A4 is the consumer (design doc §1.2).

- **Two event vocabularies, one table, prefixed keys**: `theme:<name>` (a
  `macro_detector` ThemeHit, keyword table `config/macro_keywords.yml`) and
  `fwd:<forward_events.id>` (a scheduled calendar row, already uniquely
  keyed by `uq_forward_events_key`). `load_forward_events` now returns `id`
  so the events A3 analyzes and the events §2.5 renders come from one
  loader, never two divergent queries.
- **The A2 type-boundary rule is applied harder here**:
  `l2_event_keys_for_user(session, trade_date, macro_signals) -> list[str]`
  is the only channel from per-user state into the cache (`ctx.macro_signals`
  IS per-user — `detect_macro_signals` runs over
  `load_news_window(..., user_id)`, so both the theme set and its backing
  articles depend on that user's watermark and `news_surfaced` ledger).
  `build_l2_facts(session, event_keys, trade_date)` then takes a Session,
  plain strings and a date — there is no parameter through which a
  watermark, portfolio or anomaly list COULD arrive; it re-derives theme
  evidence itself from `load_day_news`. Same rule as `ticker_intel.py`
  states for A3/A4: selection may be per-user, values must not be.
- **Day-scoped by construction** (A2's round-5 lesson): nothing here reads
  `period_start`/`period_end`. Theme evidence is one ET calendar day's
  global news; a forward event's facts are its immutable calendar row.
- **Closed-enum output, validated before storage**: the model picks
  `affected_asset_classes` from `VALID_ASSET_CLASSES`
  (`asset_class_config.py`) and `affected_sectors` from
  `sector_taxonomy.VALID_SECTORS` minus `OTHER` (`OTHER` is the bucket an
  UNCLASSIFIABLE holding falls into, so accepting it would sweep every
  unknown-sector holding into the event's exposure). Out-of-taxonomy labels
  are dropped and logged — an invented synonym would not error, it would
  intersect with nothing and turn a real exposure into a silent miss.
  `VALID_SECTORS` is derived from `_YF_SECTOR_MAP`'s values, not
  hand-listed, so it cannot drift from `map_yf_sector`'s actual output.
- **`sector` scope is NOT widened**: `affected_sectors` is stored for the
  forward-event holding-relevance mapping that already runs on `sector`
  (`report_sections._forward_exposure`) — this repo's one sanctioned use.
  The per-user exposure step (`user_event_exposure`) reads asset_class only,
  locked by a test.
- **Per-user half costs nothing**: `user_event_exposure` intersects the
  cached classes with the user's own `portfolio_summary["by_asset_class"]`
  keys. Pure set arithmetic, zero LLM calls (design doc §5.3).
- **Failure/compliance handling mirrors L1 exactly**: output is
  `_strip_markers`'d before `_scan_forbidden_output` (so a model-emitted
  disclaimer can't blacklist the day's only slot for that event); a
  violation, an API failure, or unparseable JSON writes a null-analysis
  marker row so the event is attempted at most once per day rather than once
  per user; a candidate with NO global facts is skipped without calling the
  LLM and **without writing any row at all** (an "attempted" marker would
  itself lock out a later, better-informed run the same day).
- **Daily cap fairness — TWO budgets, one per event kind**
  (`_MAX_L2_THEME_ANALYSES_PER_DAY = 10`, `_MAX_L2_FORWARD_ANALYSES_PER_DAY
  = 15`, counted separately by key prefix). Ordering alone is not enough and
  the first draft got this wrong: candidates are ordered deterministically
  and globally (sorted themes, then forward events by scheduled date —
  unlike `l1_identifiers_for_user`, which deliberately keeps the caller's
  own |move| order), but the ORDER is only stable within one user's list,
  and the lists themselves differ, because `theme:` keys are per-user while
  the `fwd:` calendar is global. Under one shared cap, the day's first
  non-quiet user could therefore be a user with no theme hits, spend the
  entire budget on calendar events, and leave every later user's themes
  unanalyzed until tomorrow (caught by blacktomb42 review round 1 on PR
  #157). Separate budgets make that impossible; truncation WITHIN a kind
  (earnings season filling the forward budget) is a genuine cost ceiling
  that truncates the same global list for everyone, not a fairness defect.
  Still NOT closed in general: a cap that binds over genuinely disjoint
  per-user candidates — L1's situation — needs batch-level orchestration and
  belongs to A4.


### Personalized assembly + fan-out budget fairness (Ring 1 stage A4, issue #128)

The consumer the first three checkpoints were built for. When
`SHARED_COMPUTE_ENABLED` is on, the §2/§3/§4 body is ASSEMBLED from the L1/L2
analyses (`report_assembly.py`) instead of inferred from scratch by one giant
per-user Pass 2 — that skipped `PRIMARY_LLM_MODEL` call is the cost
reduction, and the shape becomes `O(|identifier union|) + O(N)`.

- **The saving is the narrowed task, not a smaller model.** The assembly
  prompt never carries the raw news corpus or search snippets — those were
  already digested into L1/L2 — so `build_assembly_prompt` has no
  `news_items`/`search_results` parameter at all. Re-adding one would
  silently restore Pass 2's token profile while looking like a feature.
- **The type boundary is INVERTED from A2/A3's, because the risk is.** A2/A3
  had to keep per-user values OUT of a shared cache; A4 writes no cache — it
  READS two and mixes in per-user holdings, so its failure mode is another
  user's shared rows landing in this user's report (CLAUDE.md's §1.3
  cross-user leak, at the last checkpoint). `build_assembly_prompt` therefore
  takes **no `Session`** and `report_assembly.py` imports **no ORM model**:
  with no DB handle it cannot ask for "everything cached today", only for
  what the per-user caller passes — and that (`ctx.ticker_intel`,
  `ctx.macro_event_intel`) is already scoped by `l1_identifiers_for_user` /
  `l2_event_keys_for_user`. Locked by structural tests plus a real
  three-user fan-out assertion (`test_shared_compute_a4.py`), not by review
  attention.
- **Degradation is the default, and that is the whole safety story.**
  `_try_assembly` returns `None` — meaning "fall back to Pass 2" — for every
  failure mode: switch off, `ASSEMBLY_LLM_MODEL` unset, both caches empty,
  provider error, or a body failing the same completeness guard. The worst
  case of enabling A4 is the pre-A4 report, never a thinner one. Pass 2 still
  RAISES on a truncated body (nothing left to fall back to); assembly falls
  back in the same run. `body_is_incomplete` (`report_prompts.py`) is the one
  expression of that rule so the two cannot drift.
- **`ASSEMBLY_LLM_MODEL` is deliberately empty by default** — it is an
  OUTPUT of the shadow comparison, not an input. Enabling the switch without
  it falls back rather than guessing a model whose quality on this task
  nobody has measured.
- **Shadow comparison** (`ASSEMBLY_SHADOW_MODELS`, comma-separated): runs the
  assembly pass once per listed model over the SAME prompt, stores results in
  `report_inputs["assembly_shadow"]`, ships and emails nothing. Run it with
  `SHARED_COMPUTE_ENABLED=false` and one round yields both comparisons at
  once — architecture (shipped Pass 2 body vs each assembled body) and model
  selection — with costs read off `report_inputs["llm_calls"]`. Exception-
  isolated: a measurement harness must not be able to fail what it measures.
- **`regenerate_report(mode="analyze")` re-runs the pass that WROTE the
  body**, keyed on the stored `body_source`. Re-running Pass 2 on an
  assembled report would not just be the wrong pass — it would write
  `pass2_raw` while leaving the superseded `assembly_raw` in place, and since
  that key wins in `assembly_raw or pass2_raw`, the next `mode=render` would
  silently rebuild the OLD report. `mode="render"` stays token-free for both
  sources; pre-A4 rows carry neither key (`ReportInputsDict` is
  `total=False`) and resolve to `pass2_raw` unchanged.
- **Fan-out budget fairness, finally solved generally** (`shared_budget.py`).
  The same bug surfaced once per checkpoint — A1's Tavily budget, A2's L1
  cap, A3's L2 cap — because a shared capped daily resource consumed
  sequentially in a never-rotating user order (`active_user_ids` is sorted)
  starves the same users every day. A3's per-event-kind split only worked
  because L2 candidates group on a key prefix; L1's are per-user by nature,
  so it handed the general problem forward. The rule: **allocate from what is
  actually LEFT, divided by how many users still have to be served
  (including this one)** — `fair_share_budget(remaining, users_remaining)`,
  threaded as `generate_report(users_remaining=len(user_ids) - index)` into
  both `get_l1_intel_batch` and `get_l2_intel_batch` (which slices each of
  its per-kind budgets independently, preserving A3's split). No user can
  starve a later one; unused share flows forward because the divisor shrinks;
  `users_remaining=1` (every pre-A4 call site) means no restriction. It
  decides HOW MANY, never WHICH — candidate ordering stays per-user by
  design. Deliberately NOT a reservation table or a round-robin merge of all
  users' candidate lists: those need every user's candidates derived before
  any user's report is generated, a whole extra pass whose only product is an
  ordering.
- **"User investment context" (design doc §6.3) is scoped to the portfolio
  snapshot** — weights, concentration flags, asset-class and currency mix.
  There is no user-profile/risk-tolerance model in this codebase (that is
  Stage B), and A4 does not invent one.
- **A3's `sector` boundary holds**: the assembly path reads `asset_class`
  exposure only (`user_event_exposure`); `sector` stays scoped to the
  forward-event mapping in `report_sections._forward_exposure`.


### L3 day-level cross-name synthesis (Ring 1 quality gate, issue #128, PR #167)

The gap A1–A4 left open: L1 (per identifier) and L2 (per event) structurally
cannot express "these identifiers moved together today for one mechanism" —
three overlay comparisons on a real 26-holding book showed assembly was
otherwise as deep as Pass 2 per-name, but never produced that cross-name
sentence, because nothing in the input shape could. `cross_name_intel.py`
adds a third shared layer that performs exactly that join, once per trading
day for the whole system.

- **No per-user selection channel at all, unlike L1/L2.** What this layer
  analyzes ("every identifier the system briefed today") is already a global
  fact, readable straight from `ticker_intel`/`macro_event_intel` — so
  `get_day_synthesis(session, trade_date, ...)` has no parameter a
  watermark, portfolio, or anomaly list could arrive through. The per-user
  narrowing happens entirely on the way OUT, via `clusters_for_user`.
- **Output is decomposable clusters, not a day-level paragraph, because that
  shape is a leak-prevention property, not a formatting choice.** A summary
  naming everything analyzed today could not be narrowed to one user's
  book — it would carry other users' holdings into this report as prose no
  matter how the identifier list beside it were filtered. So:
  `clusters: [{identifiers, mechanism, summary, confidence}]`, with the
  summary required to describe the mechanism and name no identifiers.
  `clusters_for_user` additionally drops any cluster whose summary NAMES an
  identifier the reader does not hold (a prompt rule is an instruction, not
  a guarantee) — checked against the FULL day's briefed-identifier universe
  (`day_briefed_identifiers`), not just a cluster's own filtered members,
  and expanded through `holding_news.load_entity_aliases()` — a genuine
  entity/company-name subset, deliberately NOT the full
  `holding_news_keywords.yml` recall table, which mixes in theme/tech tokens
  ("gold", "lithography", "Nasdaq") a legitimate mechanism summary is
  expected to use.
- **Cache key carries an `input_fingerprint`** (sha256 over the day's
  global L1 identifier set), not just `(trade_date, prompt_version)` — a
  date-only key would freeze the day's conclusion to whichever user's
  `generate_report` reached it first, and every later user would read a
  conclusion that structurally cannot mention any of their names (the same
  "early write locks the day" shape L1's headline-only path hit once).
- **`_MAX_SYNTHESES_PER_DAY` (9) is 3x `_MAX_ATTEMPTS_PER_KEY` (3), not
  equal to it** — the first version had them equal, so one non-retryable
  failure or compliance block on the day's first fingerprint could write
  `attempt_count=3` in a single shot, zeroing the entire daily budget for
  every later, genuinely different fingerprint that day. Widening the
  constant was the accepted fix over splitting `attempt_count`'s dual
  meaning (cost tracking vs daily-budget accounting) — both concrete ways to
  split it reopen a version of the bug issue #160 already closed (a new
  flag column either lands on all three sibling tables or splits their
  semantics; capping on distinct-fingerprint/success count instead of
  `SUM(attempt_count)` lets a bad-provider day retry unboundedly across an
  ever-changing fingerprint stream). Full tradeoff write-up: Obsidian
  `Hermes/Portfonia/Docs/Ring 1-A design.md` §6.7.
- **L1's own prompt (`l1-v4`) dropped its macro-brief channel entirely,
  rather than reading L2 through a global loader.** An earlier draft passed
  `ctx.macro_event_intel` (a per-user L2 SELECTION) into L1 facts as
  `macro_briefs`, baking a per-user selection outcome into a value written
  to the shared `ticker_intel` cache — the round-5 window-leak shape in a
  new field. L3 already performs the L1+L2 join globally, so the fix was
  removing the join from L1 rather than reading L2 without contamination a
  second time.
- **`ASSEMBLY_PROMPT_VERSION` = `a4-v2`** (bumped from `a4-v1` alongside the
  new CROSS-NAME MECHANISM block, closed-set TRANSMISSION labels, TRACKING
  POSITION display rules — sub-1%-weight holdings get one line, never a
  heading, but are NOT floored out of L1 selection; deliberate legal
  tracking-position use case — and a TECHNICAL POSITION block).
- **Status**: merged (squash `c308e6c`, PR #167), three independent review
  rounds (2 bugs / 7 suggestions / 2 nits, all verified and fixed).
  `SHARED_COMPUTE_ENABLED` stays **false** — this closes the quality-gate
  structural gap, it does not itself authorize switching the production
  body-source; that is a separate, later decision.


### Narrative-layer redesign: Pass 2 material widening for large no-anomaly holdings (Ring 1 quality gate, issue #128, PR #168)

The product owner rejected assembly (L1-recap style) as the primary send
path after a 2026-08-17 side-by-side compare (26-holding book): assembly's
TSM section — 22.5% of the portfolio, +1.22% on the day but below its own
anomaly threshold — had only a moving-average line and "shares an AI capex
mechanism with other holdings"; Pass 2's own section for the same holding
had quarterly revenue, customer names, CEO commentary, and a full
Anthropic-demand -> advanced-node -> TSM transmission chain. Root cause:
Pass 2's depth comes from seeing raw search/news text and being allowed to
use public industry structure (which is what "grounded" material genuinely
supports) — assembly's design deliberately withholds both. Direction set:
**material sharing, not narrative sharing** — L1/L2/L3 stay as shared
caches, but every user's report body is still written by their own real
Pass 2 call, now fed richer material. `SHARED_COMPUTE_ENABLED` is
unaffected either way; full design rationale and iteration history: Obsidian
`Hermes/Portfonia/Docs/Ring 1-A Narrative Layer Redesign (Quality Gate Reversal).md`.

- **Large no-anomaly holdings get material too** (`large_weight_identifiers`
  in `ticker_intel.py`, top-5 by weight ≥5%, identifier strings only — same
  type-boundary discipline as every other L1 selection channel). Pass 2's
  own material-gathering in `report_generator.py` used to look only at
  `ctx.price_anomalies`; it now unions `anomaly_ids | weight_ids`, so a
  holding large enough to matter no longer needs to cross an anomaly
  threshold to get recalled news + a targeted search.
- **Weight-targeted search queries are date-locked to the report window**
  (`_targeted_weight_queries`/`_rank_title_matches_first`,
  `report_search.py`) — an unqualified `"{ident} stock news catalyst"` query
  pulled generic, sometimes months-stale articles in an early compare. The
  date lock is now enforced two ways: the query text embeds the window
  (`"{ident} stock news catalyst {start} to {end}"`, still the
  `search_cache` key) AND `_run_tavily_search`/`_fetch_one_query` accept an
  optional `date_windows: dict[str, tuple[date, date]]` param that maps to
  Tavily's real `start_date`/`end_date` publish-date filter — the query text
  alone was never enough, since Tavily itself was never told to restrict by
  date (PR #168 round 2 review). Every other caller (Pass 1, anomaly-targeted
  search, L1 leftover top-up) passes no `date_windows` and is unaffected.
- **`_weighted_identifiers` aggregates by identifier before ranking** — a
  position split across lots (this product preserves upload order, so the
  same ticker can legitimately appear as more than one `Holding` row; VOO is
  the worked example) used to be ranked as separate half-sized rows, letting
  one identifier occupy two `top_k` slots and evict a genuinely distinct 5th
  holding. `l1_identifiers_for_user`'s weight channel now calls
  `large_weight_identifiers` directly instead of re-slicing the same data
  inline, so the two call sites can't drift.
- **Combined targeted-search budget now respects `fair_share_budget`**
  (`report_generator.py`) — the anomaly + weight-targeted Tavily budget used
  to spend the full remaining daily allowance with no
  `fair_share_budget(remaining, users_remaining)` division, unlike every
  other shared-budget consumer in the same function (L1/L2/L3, and the
  leftover top-up right below it). This call runs earlier in a fan-out
  batch, so it could exhaust the day's budget before any later user — or
  even that top-up — got a turn.
- **`NAMING IS NOT ANALYSIS` no longer contradicts `GROUNDED CONNECTIONS
  ONLY`** (`report_prompts.py`, both part of `_SHARED_BODY_RULES`). The
  former told the model to write a causal chain whenever "a holding sits on
  the chain that development would transmit through" — a judgment the model
  itself would make; the latter forbids exactly that ("a plausible-sounding
  mechanism you construct yourself... is not grounding"). Rewritten to
  require the SUPPLIED MATERIAL state the exposure, and to explicitly defer
  to `GROUNDED CONNECTIONS ONLY` by name. Also fixed the `LARGE HOLDINGS
  WINDOW PRICE` reference leaking into assembly's system prompt —
  `build_assembly_prompt` never renders that section (no
  `large_holding_moves` parameter at all), so `_ASSEMBLY_SYSTEM` was
  pointing the model at data that, for that consumer, never exists.
  `_rule_direction_requires_evidence`/`_rule_naming_is_not_analysis` are now
  parameterized functions; `_SHARED_BODY_RULES_NO_LARGE_HOLDINGS` (imported
  by `report_assembly.py` instead of `_SHARED_BODY_RULES`) composes the
  no-large-holdings variant of both — duplicating only the composition
  wiring, not the rule prose, for the six rules unaffected either way.
- **Status**: merged (squash `1a831a9`, PR #168), two independent review
  rounds on the review-fix commits (1 bug / 1 suggestion / 1 nit, then
  0 bugs / 3 suggestions, plus 1 nit from an independent redundant review
  pass — all verified against actual code and fixed with TDD). Deployed to
  production (`systemd-run docker compose up -d --build`, no new migration,
  `/health` verified, 7 containers stable, clean logs). `SHARED_COMPUTE_ENABLED`
  stays **false** — this closes gaps in Pass 2's own material-gathering, it
  does not touch the assembly path or authorize turning it on.



### §1 unpriced-holding placeholder (issue #295, PR #301)

**Trigger**: a foreign-listed holding can legitimately have no fresh close
across a multi-day window (weekend + local market holiday); before this
change `compute_portfolio`'s price-missing `continue` both excluded the
holding from totals AND erased its §1 row, so the user's own report read as
data loss ("price pending" vs. "gone"). Confirmed against production
(2026-08-31 report: no PSH row in §1).

**Behavior now** (user-visible §1 change):
- Never captured (no price_snapshots row ever): row is kept with
  `[price unavailable]` placeholders in the value/weight cells, excluded
  from all totals. Placeholder renders as 暂无价格 in zh-Hans via
  `report_glossary["[price unavailable]"]` — a unique token on purpose, not
  `"N/A"`, so the glossary can't mis-translate unrelated "n/a" text.
- Multi-day stale (captured close >4 days old, `stale_priced_tickers`):
  value stays in totals and shows, with an inline `[price stale]` marker
  (价格过期).
- `stale_tickers` still drives the ops alert + bug report + §1 footnote
  (wording updated: "shown as unpriced in §1", no longer "excluded from
  report" — the row is visible now).
- Pass 2 holdings list shows `(unvalued)` instead of a fabricated `0.0%`
  for unpriced rows.

**Implementation notes**: `HoldingValue.market_value(_base)` are
`Decimal | None`; aggregates gate on `market_value_base is not None`;
`_compute_concentration` ranks only priced holdings. §1 custodian subtotals
use `or 0` (deliberate exclusion of unpriced rows from subtotal math).

**Status**: PR #301 open, awaiting review/merge.
