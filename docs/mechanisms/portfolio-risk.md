# Portfolio historical volatility and risk (issue #551)

`GET /portfolio/risk` is a read-only companion to Portfolio Overview. It does not change `/portfolio/summary` or `/portfolio/performance`. The panel appears after Unrealized P&L. The authoritative product contract is issue #551 and its Requirements, Reasons, Exploration, Design, and Contract constraints comments.

## Data and sampling

- The portfolio uses unrounded approximate-TWR `daily_links` from `portfolio_performance._build_portfolio_series`. The service excludes dates without a complete snapshot batch, dates on or before the real tracking start, and any day with an `approx_carried` snapshot row. Other snapshot quality marks follow the existing TWR calculation.
- S&P 500 close dates in `benchmark_prices` define the NYSE calendar for portfolio volatility and Beta. The selected benchmark's own close dates define its gold-line calendar. A switch of benchmark cannot change the portfolio series, Beta, or Risk.
- Each current window contains at most 60 trading dates on its own calendar. A rolling point needs at least 22 valid simple daily returns. Volatility is sample standard deviation times √252. Beta uses same-day portfolio/S&P returns in the display currency and is unavailable with fewer than 22 pairs or zero S&P variance.
- The endpoint returns status and `null` for unavailable values, never a fabricated zero. Window dates and sample counts are exposed separately for each line. An empty holdings list does not suppress independently computable benchmark volatility (owner clarification, 2026-09-24).
- Fewer than 22 valid portfolio samples makes portfolio volatility and Beta unavailable while a sufficiently sampled index can still return its gold-line value and points. Risk follows its questionnaire or insufficient-sample branch. Deviation remains based on current valued holdings and a submitted questionnaire; historical sample count does not gate it.

## Current holdings and questionnaire

`compute_portfolio` supplies the same valued holdings used by the Overview breakdowns. Manual valuation share is the valued non-cash manual and wealth-management value divided by all valued value, including cash. At 66% or more, personalized Risk reports `data_quality`; the chart, Beta, and Deviation remain independently available.

The stored `user_investment_context` row, rather than frontend form defaults, establishes a submitted questionnaire. `risk_appetite` sets the tolerance thresholds and Deviation target. `horizon=SHORT` or `objective=PRESERVATION` lowers both Risk thresholds by five percentage points, subject to the 10%/20% floors. Deviation uses valued risk-asset share, individual-stock share only for INDEX style, and non-cash value outside selected markets. It does not read free text or infer unrecorded history.

The thresholds are named constants in `backend/app/services/portfolio_risk.py`; there is no configuration or admin surface. The exact initial values and boundary examples are in issue #551 Design D3 and D7.

## UI and scope

`risk-panel.tsx` fetches the endpoint for the Overview display currency and a local benchmark selection. It renders Beta's fixed S&P gauge, questionnaire-relative Risk, structural Deviation, and the two rolling volatility lines with separate status copy and sample disclosure. If portfolio samples are insufficient but benchmark samples suffice, the chart shows only the gold line; the portfolio legend says "insufficient sample" and the benchmark selector remains available. A single chart-level insufficient-sample message appears only when both series are insufficient. Explanations are static translated descriptions, with no per-user answer or holding facts. The three locale catalogs carry all user-facing text. The panel is descriptive and makes no trading recommendation.

No migration, capture task, provider fetch, backfill, persisted selection, report change, or production data operation is part of this mechanism.
