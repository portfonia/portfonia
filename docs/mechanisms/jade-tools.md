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
Unvalued/nonpositive amounts are excluded. Within this branch, a holding with
`watch_tier` set and `shares = 0` has `excluded_reason = "watch_only"`; all other
rows retain `unvalued`. The replay and stress holding lists show "Watch only
(quantity 0); not included" in the selected locale. This label does not change
statuses, coverage, weights or `no_holdings`; watched positive quantities are
unaffected. Cash and `CASH_EQUIV` remain constant
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

## Historical stress scenarios (issues #720, #723)

The Jade-only `GET /jade/stress` endpoint applies past paths to today's
holdings at today's included market-value weights at each window's first
valid day. It reads cached data only, with no database writes, outbound calls,
request-time fill, questionnaire input or recovery metric. Currency defaults
to the user's report currency and benchmark to `sp500`; invalid benchmark
codes return 422 and non-Jade callers receive 403 `subscription_required`.
The replay and tail-risk responses and cards remain unchanged.

### Windows and storage

`SCENARIOS` in `jade_replay_config` is chronological: dot-com (peak
2000-03-24, trough 2002-10-09; window 1999-09-24–2003-04-09), financial
crisis (2007-10-09, 2009-03-09; window 2007-04-09–2009-09-09), COVID (peak 2020-02-19,
trough 2020-03-23), rate hikes (2022-01-03, 2022-10-12), and tariffs
(2025-02-19, 2025-04-08). Windows extend six calendar months before the peak
and after the trough, clamping month ends: 2019-08-19–2020-09-23,
2021-07-03–2023-04-12 and 2024-08-19–2025-10-08. SPY points inside each
window define the sample calendar. An open window, an attempted empty calendar
and nonpositive proxy-chain factors are outside the handled states.

Migration `d72000000001`, following `d71400000001`, creates shared
`jade_scenario_series` keyed by `(scenario_id, series_key)` and
`jade_scenario_points` keyed by `(scenario_id, series_key, trade_date)`.
The points' composite foreign key cascades on series deletion. Series retain
attempt/success dates and unusable reasons; points store numeric total-return
closes and optional raw NAV. No existing table changes. Scenario ids are text
without a CHECK constraint.

`fill_scenarios` runs after the existing replay fill, only when active Jade
users exist, using the rebuilt instrument keys plus scenario-only substitute
keys. Substitute keys never enter replay storage or its provider batch. It logs its own summary;
the replay's writes, summary and task return value remain unchanged. Every
missing yfinance symbol is fetched in one batch per scenario from window start
minus ten days through window end inclusive. Funds use LSJZ full-window paging
with no incremental stop, building the same distribution-adjusted NAV index
as replay. An unknown distribution marks and clears only that window's pair.
Each pair commits independently; exceptions roll back that pair. Successful
or unusable pairs are never fetched again; empty/failed pairs retry nightly.
New holdings therefore enter every scenario on the next nightly run. There
is no new Beat entry; the existing 22:15 ET entry remains heavy.

### Classification and formulas

Unvalued/nonpositive holdings are excluded, using the same `watch_only` reason
for watched zero quantities as replay. Other excluded valuations remain
`unvalued`. An unattempted SPY calendar makes
all positive holdings pending. Cash/CASH_EQUIV uses currency values, and
wealth-management products are cash-assumed. Auto instruments are pending
until attempted. Own history starting within ten days of the window start
uses own prices or fund NAV; later history uses a head proxy. Attempted empty
or unusable own history also uses a head proxy for the whole window, except
CNY/CNH bond funds, which are cash-assumed. Manual instruments use the replay
proxy without beta (or cash-assumed for CNY/CNH bond funds). Missing proxy
attempts are pending. Proxy and benchmark chains choose the first candidate
with a price on both peak and trough, using the ten-day carry. An unattempted
candidate stops resolution as pending; when every attempted candidate lacks
coverage, resolution is unavailable.
Prices and each USD-pivot FX leg carry for at most ten calendar days.

A head proxy uses one beta per holding per request, estimated from the existing
five-year replay calendar in proxy currency: sample covariance / proxy
variance, with at least 60 pairs; missing/unattempted/unusable replay data
produces beta 1 and zero samples. Before own history, the forward chain starts
at 1 and multiplies by `1 + beta * proxy_return`. It switches to own returns
only when the previous computed day is already in own history. Unavailable
days preserve the last computed anchor. The chain then converts to display
currency. Only holdings with resolved values on both peak and trough enter
that scenario; others are `data_unavailable`.

With included current value `total`, weights are `w_i = current_value_i/total`.
For the first common valid day `s0`, `V(d) = sum(w_i * X_i(d)/X_i(s0))`, so
`V(s0)=1`, with no rebalancing. Peak-to-trough return is `V(T)/V(P)-1`.
Its amount is the six-decimal serialized return times `total` rounded to
cents, then half-even cent rounding. Maximum drawdown and its dates come from
the whole valid path with earliest ties. Asset-class contribution is
`sum(w_i * (X_i(T)-X_i(P))/X_i(s0))/V(P)`, ordered ascending with class-name
tie breaks. Contributions sum before rounding, without rounding adjustment.
Maximum drawdown and contributions are percentages only.

Curve points are `V(d)/V(P)-1`, with 0% on the S&P 500 peak. The resolved benchmark series
uses its converted prices over the same window. Missing
attempts are pending; unresolved peak/trough values after ten-day carry, or
fewer than ten returns, make it unavailable without changing portfolio
figures. A point one day before the peak resolves that peak. Benchmark figures
are percentages only. Statuses are no_holdings, pending, insufficient below
ten returns, and ok. Non-ok figures are null with empty curves/contributions.
Coverage uses included value; approximation at 66% gives a notice and any
proxy/head-proxy share gives the separate proxy-understates notice. Scenario
responses add `fx_source`, resolved benchmark symbol/name and price-only flag,
`substitutions` and sorted unique `price_index_symbols`. Holding rows add
`proxy_for` and `price_only`. Substitutions list included holdings in book
order with duplicate (primary, symbol) pairs removed, then an available
benchmark. Excluded holdings do not contribute to disclosures. The top-level
benchmark symbol/name still describe the primary ETF.

### UI and operations

`StressSection` follows replay currency/benchmark below tail risk, with single
flight and latest-settings queueing. Span/scenario switches make no request.
Failure retains previous data and shows an alert without automatic retry.
Five chronological scenario buttons select the last response scenario by
default (2025 tariffs before data). Selection is local. An amber approximation
notice sits above the figures for ok/insufficient scenarios when data-quality,
substitution or price-index disclosure applies. It always states the
approximated share, then conditionally the quality text, substitutions and
price indexes without dividends. The plain quality paragraph is replaced by
this box; proxy-understates text remains. Per-holding and model disclosures
explain substitutes, primary-ETF beta estimation and FRED FX where applicable.
Prices use total return except the named price indexes, which omit dividends.

The calculating overlay wraps the disabled initial scenario buttons, table,
fixed chart frame, contribution tracks, notices and model. Title/intro and
load error sit outside. Selector, legend and table text wrap at 375px; chart
and tracks fill the card. Model details start closed at every width. The
chart marks S&P 500 peak/trough, and contributions extend left/right of a
center line. All three locales disclose proxy methods, coverage, today's
holdings/weights, FX, total returns and hindsight; this is a replay, not a
forecast.

Deployment/migration, FX extension, USDCNH backfill and manual nightly fill
each require separate owner authorization. After deployment the ordered
operations are: `backfill_fx_rates --years 8 --before-earliest` and earliest-date
readback; `backfill_usdcnh_history --years 8` dry run with separately authorized
`--apply`; then wait for nightly fill or separately trigger the existing task
and read scenario success counts. Missing CNH history stays unavailable.

### Substitute configuration and gold data (#723)

`backend/config/jade_substitutes.yml` contains ordered substitutes keyed by
all 18 `FIXED_ETF_SYMBOLS`; an empty list means no substitute. The
`jade_substitutes` module validates at import with `yaml.safe_load` and raises
`ValueError` for malformed mappings/lists/entries, missing or extra ETF keys,
unknown or missing fields, invalid symbol/name/source/currency/bool values,
invalid or missing file names, repeated/primary symbols in a chain, or a
symbol whose specifications differ across chains. File sources must exist
under `config/jade_scenario_data`. There is no hot reload.

New user instruments require no config change: replay classification maps
them to the existing primary ETF chain. Only a new proxy/benchmark ETF
requires a config entry, enforced by the exact key-set validation. Beta is
still estimated against the primary ETF's five-year replay history; scenario
prices and FX use the resolved substitute's currency, including the own
history conversion in a head-proxy chain. No dividend adjustment is made
for price indexes. Nasdaq can resolve to `^IXIC`, and CSI 300 to `000001.SS`.

The committed `backend/config/jade_scenario_data/lbma_gold_pm.csv` is the
London afternoon LBMA gold fix in USD, redistributed from:
https://raw.githubusercontent.com/unbalancedparentheses/forex-centuries/70cbfae9610381ace1a893f7beb143fc1de991b5/data/sources/lbma/lbma_gold_daily.csv

Pinned commit: `70cbfae9610381ace1a893f7beb143fc1de991b5`.
Build filter: `1999-09-14 <= date <= 2003-04-09` and `gold_pm_usd > 0`,
ascending; header `date,close`, preserve the source price text, LF line endings
and a trailing newline. There are 896 rows, from `1999-09-14,256.75` to
`2003-04-09,321.35`. SHA-256:
`e53708d298c5c0ec7d081781795463211a7950d9b028ab7cb1a820f505b21fc8`.
The build script is not committed. The file is read locally by scenario fill;
successful pairs are skipped thereafter. Its empty results in the other four
windows remain failed and are read again nightly, as are empty Yahoo
substitute results in the existing per-scenario batch. No extra retries,
alerts, cache, Beat entry or migration is added.

### One-time FRED FX operations (#723)

`python -m app.scripts.backfill_fx_fred` is a dry run; `--apply` inserts in
chunks with `ON CONFLICT DO NOTHING` and commits. It fetches 12 H.10 series
with a 30-second timeout, skips missing observations and dates outside the
two windows including the ten-day fetch margin. EUR, GBP, AUD and NZD are
inverted to `1 USD = X`; other series are direct. CNH uses CNY (`fred_cny`),
and MOP uses HKD × 1.03 (`fred_hkd_peg`); direct rows use `fred`. Every
existing row retains its rate, source and fetched_at. Output includes counts,
first/last dates, existing window counts, and an earliest-row comparison for
existing direct pairs. A failed request skips its pair and derived pair,
prints `[ERR]`, and gives exit status 1; no nightly FRED job exists.

Each production step requires separate owner authorization: deploy; export
FX rows inside the windows as a backup; run/review the dry-run counts and
comparison lines; apply and read back per-pair counts/ranges plus unchanged
row counts outside the windows; wait for nightly fill or separately trigger
it and read back the two scenario success counts; owner phone check at 375px.


## Style exposure (issue #727)

The Jade-only, read-only `GET /jade/style` endpoint describes today's unchanged
holdings with Sharpe's returns-based style analysis. Currency defaults to the
user's report currency, benchmark to `sp500`; non-Jade access returns 403
`subscription_required` and invalid benchmark values return 422. There are no
request-time writes, provider calls or fills.

### Basis and cache

The ordered basis is IWF (US large-cap growth), IWD (US large-cap value), IWM
(US small-cap), EFA (developed markets ex-US), EEM (emerging markets), 2800.HK
(Hong Kong), 510300.SS (China A-shares), AGG (US aggregate bonds), TLT (US
long-term Treasuries), GLD (gold), DBC (commodities), VNQ (US real estate) and
BIL (USD cash and short-term bills). It covers recognizable styles and asset
classes without the near-duplicate quality, momentum or minimum-volatility
ETFs. TLT distinguishes duration from AGG; there is no separate CNY bond basis.

`STYLE_KEYS` is separate from `FIXED_ETF_SYMBOLS`, which remains unchanged along
with the substitute YAML and its exact-key validation. Nightly replay fill uses
`instrument_keys | STYLE_KEYS`, adding IWF, IWD, IWM, TLT and BIL to the existing
five-year cache and single Yahoo batch. Scenario fill still receives only
`instrument_keys | substitute_keys()`; an ETF held by a user remains an instrument
key. With no active Jade users the existing early return is preserved. There is
no new dependency, table, migration, Settings field or Beat entry.

### Samples, solver and fit

`compute_style` uses unrounded `build_replay(..., "3M")` values. Basis adjusted
closes and historical FX carry at most ten calendar days, using the replay's
`price_on` and USD-pivot `to_base`. Common dates include only portfolio dates
with all 13 converted basis prices. Each return compares positions three apart:
`P(D[i])/P(D[i-3])-1`, with the same formula for every basis column. Skipped
dates are spanned by position. Samples overlap to align asynchronously closing
markets; their effective independent count is smaller than the displayed count.

The numpy primal active-set solver minimizes `||y-Xw||²` with `w >= 0` and
`sum(w)=1`. It starts at the closest single-column vertex, solves the free-set
KKT system, releases the most negative fixed-index multiplier, or steps to the
first nonnegative boundary and removes zero weights. Ties follow basis order.
Tolerance is `1e-12`; failure to converge in `10*k` iterations raises
`RuntimeError`. Singular KKT matrices have no special data-state branch. Tests
compare seeded 3-, 8- and 13-column problems with an independent exhaustive
support-enumeration oracle and independently check simplex/KKT conditions.

Residuals are `e=y-Xw`. R² is `1-variance(e)/variance(y)` with sample variances;
it is null for zero portfolio variance and can be negative. Unexplained
volatility is `stdev(e)*sqrt(252/3)`. The low-fit flag uses unrounded R² strictly
below 0.60, excluding null. All 13 weights and ratios serialize independently
to six decimals without rounding adjustment. The curve is a fixed mix at the
first common date, `sum(w[j]*B[j](d)/B[j](D[0]))-1`, without rebalancing.

Status order is replay `no_holdings`, replay `pending`, missing/unattempted
basis `pending`, replay insufficient or fewer than 42 three-day returns
`insufficient`, then `ok`. Non-ok results have null fits and empty points,
retaining replay coverage and window fields. Benchmark fitting runs only for
an ok portfolio, on its own common dates and 42-sample gate; pending remains
pending and insufficient benchmark data is unavailable. For a non-ok portfolio,
benchmark status is copied from replay and its fit is null.

### Card and notices

`StyleSection` follows successful replay currency/benchmark settings below the
stress card, only for active Jade subscribers. It has no span selector or
questionnaire input. Requests use one flight and queue only the latest settings;
failure retains previous data and an alert without automatic retry.

The three-column table shows positive-weight rows in either fit, descending by
portfolio weight with basis-order ties, portfolio bars, benchmark percentages,
R² and annualized unexplained volatility. The amber notice appears only for
low fit. Both R² columns and the low-fit notice floor to two decimals after
rounding to six-decimal integer precision: 0.597000 → 0.59, 0.290000 → 0.29,
and -0.351000 → -0.36. The 240px curve compares portfolio and style mix. The
model details start closed at every width. Table cells wrap, bars and chart fill their cells,
and dark-mode notice classes are checked at component level in a 375px wrapper.

All three locales explain the fixed three-month window, overlapping samples,
statistical equivalent rather than actual holdings, FX, total returns and
historical limitations. The replay 66% approximation notice is preserved, and
positive proxy or head-proxy share separately explains mechanically inflated
fit. No position judgement or action wording is added. Review, merge, deployment,
nightly-fill triggering and each production operation remain separately
authorized. Until nightly fill attempts the five new series the card is pending.
