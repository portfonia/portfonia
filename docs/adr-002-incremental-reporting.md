# ADR-002: Incremental reporting with a continuous capture layer

Status: Accepted — 2026-06-07 (Ring 0)
Supersedes the fixed weekly-window report behaviour for the report cadence.

## Context

The first full weekly run (June 7 2026) exposed that the "weekly" report is
really a daily-windowed snapshot: news is a live `past 24h` RSS pull and price
anomalies compare the last two daily closes (a 1-day move). Two structural facts
drive this ADR:

- **RSS is amnesiac.** Feeds carry only ~1–2 days of items, and `fetch_news`
  returns an in-memory list — nothing is persisted. A multi-day window cannot be
  reconstructed at report time from a live pull.
- **Prices are recoverable.** yfinance serves daily history, but holdings store
  only the latest `market_price`; there is no price history table.

The asymmetry — news must be stored, prices need not be — shapes the design.

## Decision

Split the system into two layers.

### 1. Capture layer (global, market-session-node schedule, catch-up in task)

Persists the raw inputs so windowed reports can query them later. Global, not
per-user (news and market prices are not user-specific). Costs **no credit**:
RSS and yfinance are free; macro detection is keyword-based (no LLM). Credit is
spent only in the report layer.

- **Storage**
  - `news` table — long-term knowledge base (retention **1 year**), the future
    substrate for mempalace vector/KG enrichment. Stores RSS summary, not full
    article body. Cross-run dedup by `url_hash`.
  - `price_snapshots` table — retention **1 year**. Close node stores daily
    OHLCV; intraday nodes store best-effort `last` (nullable when yfinance has no
    intraday/extended-hours data). Unique `(ticker, market, session_node, trade_date)`,
    upserted (idempotent catch-up).
  - FX continues to use the existing `fx_rates` table (daily), not duplicated
    into `price_snapshots`.
- **Capture nodes** (no call-auction, no trading calendar — closed days simply
  store nothing):
  - US: pre-open 09:00, open 09:30, close 16:00, after-close 20:00 ET
  - HK: open 09:30, close 16:00 HKT
  - CN A-share: open 09:30, close 15:00 CST
  - News fetched at every node. FX once daily at US close.
- **Scheduling (DST-correct):** US nodes via ET crontab (DST-aware); HK/CN nodes
  via fixed UTC crontab (those markets do not observe DST). Beat fires
  forward-only, so **catch-up lives in the task body**, not Beat: each fire
  covers `[last successful capture watermark, now]`. Prices are backfillable from
  yfinance; news is only recoverable within the RSS horizon (~2 days) — a longer
  news outage is a permanent thin spot (low risk: the dev machine does not sleep).

### 2. Report layer (per-user, incremental window)

- **Window = `[previous report.period_end, now]`** for that user. The watermark
  is **derived** as `max(period_end)` over the user's prior reports — not a
  separate mutable pointer. Therefore deleting/superseding a report rolls the
  watermark back automatically, and `regenerate_report` keeps a report's stored
  period fixed (reproducible). This satisfies the "#6 rollback must also roll
  back this state" requirement without extra machinery.
- **Cold start** (no prior report): `period_start = 2026-06-01 16:00 ET` (US
  regular close), a fixed bootstrap constant.
- **Cadence:** M/W/F at **16:30 ET** (after US *regular* close — not after-hours).
  A missed report needs no special catch-up: the next run's window is "since last
  report", so it naturally widens to cover the gap.
- **`report_type`** column is kept for future fixed-window types; Ring 0 uses the
  single value `"incremental"`.
- **Anomaly threshold (variable window):** Ring 0 uses a flat `%` move since the
  baseline close and states the actual number of trading days the report covers.
  *Future:* threshold = flat% × trading-days-in-period, capped at 10% (any move
  > 10% is always flagged).
- The Pass 1 → Tavily → Pass 2 → annotate → compliance-scan → render(translate)
  pipeline is unchanged except that its news/price inputs come from the captured
  stores over the window, and the prompt/`#5` header state the real window.

## Consequences

- Capture frequency is free; the only credit lever is report cadence (3/week).
- New-position handling: a ticker added after the last watermark has no baseline
  snapshot → reported as "new position, no prior baseline", not an anomaly.
- The dev machine must stay awake for capture cron to fire (confirmed: it does);
  OCI residency removes this dependency at Ring 1.

## Build sequence (each step ships green)

1. `news` + `price_snapshots` tables (models + migration). ← this commit
2. Capture tasks + session-node schedule (ET + fixed-UTC) + in-task catch-up.
3. Report reads news/prices from the stores over the incremental window.
4. `period_start`/`period_end` persisted on `reports`; watermark derivation.
5. M/W/F incremental report schedule.
