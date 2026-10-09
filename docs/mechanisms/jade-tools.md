# Jade portfolio tools

## Holdings replay (issue #714)

Holdings replay answers how today's unchanged holdings would have moved over
a selected span, up to five years (issue #716). It is a hypothetical total-return
replay, not the user's tracked history. The Jade page renders the section only for an active Jade subscriber,
including a pending cancellation. The questionnaire is not used.

### Shared cache and nightly fill

`jade_price_series` identifies shared instrument history as `yf:<symbol>` or
`nav:<fund_code>`, with ET attempt/success dates and an optional
`unparsed_distribution` reason. `jade_price_points` stores a numeric total-return
close per series/trade date; NAV points also store the raw unit NAV. These keys
contain no user id. Migration `d71400000001`, following `d71000000001`, creates
both tables; downgrade drops points before series.

At 22:15 ET, `refresh-jade-price-history-daily` runs
`refresh_jade_price_history_task`. Its agent quiet-window classification is
heavy. With no active Jade users, it writes nothing and makes no outbound call.
Otherwise it fills the fixed proxy/benchmark ETFs and only those subscribers'
auto-priced, capture-supported tickers/fund codes. Tickers take precedence and
use the existing legacy normalization.

The yfinance helper downloads the entire five-year window plus a 15-calendar-day
margin with adjusted closes in major currency units. The whole window is
upserted nightly because dividends/splits can rebase earlier adjusted history.
Missing symbols retain their existing points and success date, while their
attempt date advances.

Eastmoney LSJZ is paged newest first with `pageSize=20`, pausing 0.2 seconds
between pages. An initial fill stops at an empty page; incremental fills stop
at the page containing the last cached date. A failed page invalidates that
fund's fetch and leaves its existing points intact. The index starts at unit
NAV and continues as `I = I_prev * (N + cash_distribution) / N_prev`, parsing
cash distributions per ten units using the issue's exact pattern. An unknown
nonempty distribution logs a WARNING, marks the series unusable and deletes
its points. It remains unusable until an operator clears the reason after
supporting that format. Each series commits independently. No notification or
capture-health integration is added.

### Classification and valuation

`compute_replay` uses `compute_portfolio`'s current valuations and book order.
Unvalued/nonpositive amounts are excluded. Cash and `CASH_EQUIV` remain constant
in their own currency; wealth-management products are treated as cash. An auto
instrument is pending until its cache is attempted. Usable own history reaching
the start within ten days uses own prices or the fund NAV index. Later-starting
history uses a beta-scaled ETF fill before its first point. Attempted but empty,
stale or unusable own history falls through to a proxy (or cash for CNY/CNH bond
funds), with the missing-own-history disclosure.

Proxies are the named ETFs in `jade_replay_config`: individual stocks use their
market ETF; other asset classes use their class ETF. USD and other non-CNY/CNH
bond funds use AGG. An unattempted proxy is pending; an unavailable proxy is
excluded. Every included instrument must also have its terminal display value
available, including all required historical FX legs, within ten days of the
window end. Failure of that check excludes it as `data_unavailable`.

### Calculation

The latest S&P 500 date on or before today ET ends the window. Spans are 1M,
3M, 6M, YTD, 1Y, 3Y and 5Y, with 1Y as the default. Month spans subtract calendar
months and clamp to the destination month's last day; year spans subtract
calendar years (Feb 29 becomes Feb 28). YTD starts at the latest stored S&P 500
close strictly before Jan 1 of the end's year, including the first trading day's
return. Stored S&P dates define NYSE sample days.
Prices and each USD-pivot FX leg carry forward for at most ten calendar days.
No interpolation or request-time provider fetch occurs.

For a later listing, own returns are converted into the proxy's currency. Beta
is sample covariance divided by proxy sample variance over consecutive common
sample-date pairs, with at least 60 pairs and positive proxy variance; otherwise
it is 1. Before listing, local values roll backward by
`Y_prev = Y / (1 + beta * proxy_return)`, then convert to display currency.
Disclosure includes sample count, own volatility and filled-segment volatility.
Missing proxy pairs produce unavailable dates; subsequent available pairs use
the last computed anchor. An empty NYSE calendar and nonpositive backward-fill
factor are deliberately outside the issue's handled states.

Each instrument's current amount `m` rolls backward as `m * X(d) / X(E)`.
Amounts are summed without rebalancing; any unavailable included instrument
makes that date unavailable. Returns compare consecutive valid values, including
across skipped dates. Metrics use the same function for the portfolio and the
selected total-return ETF: cumulative return, annualized return using actual
calendar days, sample volatility times sqrt(252), maximum drawdown with earliest
tied peak/trough, worst day and worst calendar month. The first month's base is
the first valid value. For 1M, both portfolio and benchmark worst-month values
and labels are null. Annualization uses the selected span's own data, including
short periods. Ratios serialize to six decimal places and beta to four.

Coverage weights use included positive value. Approximation at the start is the
sum of proxy, head-proxy and cash-assumed shares; at 66% it adds a notice without
suppressing results. Pending share divides pending value by included plus
pending value. With no included instruments, coverage shares are zero and the
data-quality notice is false; pending share retains its normal calculation.

### API and UI

`GET /jade/replay` accepts optional `base_currency` (default: the user's report
currency), `benchmark=sp500|dow30|nasdaq|csi300` (default `sp500`), and
`range=1M|3M|6M|YTD|1Y|3Y|5Y` (default `1Y`; invalid values return 422).
The response echoes `range`. Access uses
`is_jade` exclusively, returning 403 `subscription_required` for other callers;
there is no rate limiter, outbound call or database write. The selected
benchmark ETFs are SPY, DIA, ONEQ and 510300.SS, respectively.

Statuses are `no_holdings` when no amount is positive, `pending` when all positive
holdings are excluded, `insufficient` below ten valid returns, and `ok` otherwise.
`points` is a list, empty unless `ok`; portfolio metrics are null unless `ok`.
A pending benchmark or one with insufficient history has null metrics and null
benchmark values in the curve. Benchmark availability requires ten valid
returns from the first benchmark value on or after the portfolio start.

The Jade card contains page-local span/currency/benchmark controls, a 240px numeric
time-axis cumulative-return chart, three-column metrics and stacked holding
method disclosures. The initial request explicitly selects 1Y. Controls disable
during a request; span, currency and benchmark revert to the last response on
failure. The shared `CalculatingOverlay` covers settings, chart, legend, metrics,
notes and the data-and-method block during initial and subsequent requests. Its
pointer layer and inert child region block repeated interaction; intro and
load-error alerts remain outside. The empty chart frame, six-row metrics grid
with dashes, and details summary render immediately. Static sources, method
(using the currently selected currency) and limitations paragraphs always render
inside details. Window/sample data, coverage/notices and holdings render only
for `ok` or `insufficient`. For `ok` short spans (1M/3M/6M/YTD), a note explains
that annualization magnifies short-period swings. Null worst-month cells show a
dash with no label line. No selection is persisted.
Dashed lines mark the start of each head-proxy holding's own history. Data and
method details default open at 640px and above, closed on mobile. The settings
wrap, the chart fills the card and grid/list text wraps for 375px dark layouts.
Replay copy is keyed under `jade.replay`, with the shared status label under
`common.calculating`, in the three locale catalogs, with source,
method, approximation and hindsight limitations adjacent to the results.

The existing Portfolio Overview, Risk, Performance, reports and their data
pipelines remain unchanged. Review, merge, deployment, production migration and
manual production fill each require separate owner authorization.

## Tail risk (VaR / CVaR) (issue #718)

`GET /jade/tail-risk` is a read-only Jade-only endpoint, using the same default
report currency and `benchmark=sp500|dow30|nasdaq|csi300` as replay. It has no
span selector, write, provider call or request-time fill. `build_replay` exposes
its unrounded portfolio and benchmark values and included current value; the
existing replay response stays unchanged. Tail risk always builds the 5Y span,
regardless of the replay card's selected span.

Historical simulation sorts simple daily returns ascending. At confidence
`c = 95|99`, the tail contains `k = ceil(n * (100-c) / 100)` observations,
computed with integer arithmetic. VaR is the negative of the k-th return;
CVaR is the negative mean of the first k returns. Positive ratios represent
losses; a tail gain remains a negative ratio. Actual monthly returns use every
pair of valid values 21 positions apart. Windows overlap; the model also gives
`floor(daily_count / 21)` as the approximate non-overlapping count. Neither
historical horizon uses square-root-of-time scaling. Ratios have six decimals;
portfolio amounts multiply those serialized ratios by the included current
value rounded to cents, with half-even cent rounding. Benchmark cells contain
percentages only.

The 99% portfolio level needs at least 500 daily returns, independently of the
benchmark's own 500-return gate. Replay statuses and coverage are copied.
Non-ok responses have empty levels/histogram and null portfolio value. Zero
monthly windows yield null monthly cells and tail count. One window produces
historical monthly values but no normal monthly comparison; sample standard
deviation requires two windows. Benchmark monthly availability uses its own
windows. Missing cells show a dash with no additional short-sample notice.

The normal comparison uses the observed mean and sample standard deviation:
`VaR = z*sigma - mean` and `CVaR = sigma*phi(z)/(1-c) - mean`, where `c` is the
confidence ratio and `z` its standard-normal quantile. It appears only inside
the calculation model. Valid existing questionnaire answers supply the shared
Overview Risk upper volatility threshold through `risk_answers_valid` and
`risk_thresholds`. The reference assumes zero mean: daily `z*threshold/sqrt(252)`
and monthly `z*threshold*sqrt(21/252)`. It remains available when portfolio 99%
is gated, and is a comparison line without caution or position judgement.
Missing/invalid answers show a questionnaire link; there is no questionnaire
change or new logging.

The card follows successful replay currency/benchmark responses and queues only
the latest settings during a request. Span changes and the local 95%/99% toggle
make no tail request. On failure it retains prior data, displays an error and
waits for a settings change. The calculating overlay covers the initial toggle,
three-column table, histogram, legend, notices and model; title, explanation and
load error remain outside. Model details stay closed initially at every width.
The histogram uses 0.005-wide bins including empty bins, marking the selected
daily VaR/CVaR and questionnaire reference when present. Portfolio figures are
rendered with reversed signs as percentage and amount, benchmark figures as
percentage only. The table cells and legend wrap and the histogram fills the
card at 375px in dark mode.

The 66% approximation notice is preserved, with a separate proxy-understates
notice whenever proxy or head-proxy value is positive. The model states window,
samples, coverage, overlapping windows, formulas, reference derivation and
hindsight/period limitations. No backtest, result cache, dependency, settings,
migration or nightly-fill change is introduced. Review, merge, deployment and
production operations remain separately authorized.
