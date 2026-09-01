# Compliance/ops alerting and asset classification

### Compliance + ops alerting (current state)

- `_FORBIDDEN_OUTPUT_PATTERNS` (single source of truth:
  `app/compliance/forbidden_vocab.py`) targets only direct advisory/action
  vocabulary — stop-loss, strong-buy, target-price, investment-advice, and
  their zh-Hans equivalents (exact patterns, including context-aware regex
  for terms with legitimate non-advisory uses, live in
  `config/compliance_vocab.yml`, loaded by the source file).
  Descriptive TA-observation terms (support/resistance, etc., EN + zh-Hans —
  see `ta_observation_terms` in `i18n_glossary.yml`) are explicitly
  allowed — see "Forbidden vocabulary" below for the Layer-4 line.
- Disclaimer `f3-bilingual-v2`: names the AI LLM generator explicitly, plus
  imprecise-language and sender-no-liability caveats, EN+zh.
- `send_ops_alert(subject, body)` (`email_sender.py`) sends plain-text to
  `ADMIN_EMAIL` (default `portfonia@gmail.com`) on `needs_review` or
  final-retry failure in `generate_incremental_report`.
- GitHub issue auto-creation (`app/services/github_issues.py`) fires
  alongside ops alerts for events indicating code/data bugs (stale_tickers,
  capture final failures, generation final failure). Requires
  `GITHUB_TOKEN` (PAT, `repo` scope) + `GITHUB_REPO`; silently skipped if
  absent.
- All capture tasks (news/prices/fx/fund_navs/forward_events) send ops alert
  + GitHub issue on final-retry exhaustion.


### Asset classification + fund NAV capture

`asset_class` is the economic-exposure dimension (distinct from the
LLM-parsed `asset_type` product form) — classified by underlying exposure,
not listing location. **The class list and every number are defined in code
+ config, not here** — do not let this table drift out of sync again
(it did, twice, on 2026-06-20):

- Class list: `VALID_ASSET_CLASSES` in `app/services/asset_class_config.py`.
- Per-class numbers + per-class rationale comments: `config/asset_class_thresholds.yml`.
- Which holdings map to which class: `config/ticker_asset_class.yml`, loaded
  fresh per classification by `holding_parser._load_ticker_asset_class()`
  (issue #296 — hot-reloadable, no process restart for a new fund_code;
  unknown asset_class values fail loudly at load, mirroring the thresholds
  file's closed-taxonomy check).

`ticker_themes` table maps ticker/fund_code → theme for multi-holding
aggregation (e.g. QQQM + 019547 both `nasdaq_100`). Seeded themes:
`nasdaq_100`, `sp500`, `gold`, `japan_equity`, `tbill`.

Fund holdings (fund_code only, no ticker) participate in anomaly detection
via Tiantian Fund historical NAV: `fund_nav_fetcher.fetch_nav_history()` (lsjz API)
→ `price_capture.capture_fund_navs()` upserts into `price_snapshots` keyed by
fund_code. Beat task `capture_fund_navs_task` runs 20:00 CST Mon-Fri (NAV
publishes same evening after A-share close). Confirm-time
`backfill_fund_navs_task` covers funds that have never been captured
(issue #196) so a first report does not wait until that beat.
`detect_window_anomalies` identifier fallback chain: `h.ticker or h.fund_code`.


### §1 / distribution / §4.1 now read `asset_class`, not sector (2026-06-19)

`portfolio_calculator.py` adds `PortfolioSnapshot.by_asset_class` (every
holding, no "Other" fallback) alongside the older `by_sector`/`by_asset_type`
(kept only for the API and forward-event sector mapping, no longer rendered
in reports). §1's table column, the distribution block, and §4.1
concentration's top-bucket check all switched to this dimension — sector is
a stock-picking lens with no allocation guidance, and `asset_type` (ETF vs
Fund) split holdings that wrap the same underlying exposure.

§4.1 top-holding/top-3 ranking stays **per-row, unmerged** (deliberate
design choice); only the asset_class *bucket* check merges the same exposure
across markets (e.g. VOO + 513650.SS both land in `EQUITY_US_BROAD`).
Single-holding watch/high thresholds are differentiated by the top
holding's own asset_class. Top-3 stays flat (>50% watch). Top-asset-class
bucket is flat (>50% watch, >65% high) since the bucket already pools every
holding sharing one exposure. `Concentration.top_sector_*`/`sector_watch`
were removed (replaced by `top_asset_class_*`/`asset_class_watch`/
`asset_class_high`) — this is a breaking schema change on
`/portfolio/summary`, acceptable at Ring 0 (no external consumers). Root
cause + before/after: GitHub issue #32.


### Asset_class thresholds are admin-configurable (#35)

Every per-class number (anomaly per_day/cumulative_cap, concentration
watch/high) lives in `config/asset_class_thresholds.yml`
(`Settings.ASSET_CLASS_CONFIG_PATH` override), loaded fresh on every call —
**an admin edit takes effect on the next report, no process restart**. Read
that file directly for current values and the rationale behind each one
(it carries a comment per class); do not copy numbers from it into this
file. The loader (`app/services/asset_class_config.py`) validates the
YAML's class keys exactly match the closed taxonomy in
`VALID_ASSET_CLASSES`; adding a new category is a **code change**, not a
config edit — and existing holdings/`ticker_themes` rows already classified
under the old category need a backfill migration (see `8c9d0e1f2a3b` for an
example) or they'd silently inherit the wrong tier. Per-user threshold
overrides are a Ring 1 decision, documented in `Portfonia Concept & Design.md`, not
built yet.


