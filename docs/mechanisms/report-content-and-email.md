# Report content features and email HTML rendering

### Report content features (Ring0 #1-4 + R-3/R-5/R-6/R-7/R-8)

All numbers are **code-built and stored in `report_inputs`** (deterministic,
re-render-safe); the LLM writes only prose/attribution. Current shape:

- **§4.2 price-anomaly table** — session-arc numbers rendered as a markdown
  table; LLM writes one driver line per holding, restricted to a "see §4.2"
  cross-reference (exact EN/zh-Hans wording in `i18n_glossary.yml`'s
  `templates.cross_reference_example`) only for holdings actually in the table.
- **Confidence labels** — every causal attribution ends with
  `[Established]/[Probable]/[Speculative]` (never a numeric %); zh-Hans
  renderings defined in `i18n_glossary.yml`'s `report_glossary`.
- **§4.4 technical position** (`technical_position.py`) — descriptive OHLCV
  facts only (distance to 50/200-day avg, 52-week range, 20-day vol); TA
  signal vocabulary (support/resistance/golden-cross/death-cross, EN + zh-Hans
  — see `ta_observation_terms` in `i18n_glossary.yml`) is forbidden in the
  body. Needs ~200 captured closes — seed once via
  `python -m app.scripts.backfill_ohlcv`, or let `confirm_holdings` dispatch
  the scoped `backfill_ohlcv_task` for this user's sparse tickers.
- **§2.5 forward calendar** (`forward_events.py`) — US macro releases (FRED,
  optional `FRED_API_KEY`), hardcoded FOMC dates (verify annually against
  federalreserve.gov — FRED has no forward FOMC schedule), earnings via
  yfinance. Calendar facts only, no forecasting. China forward intel out of
  scope. T+0 events get a lead-note promotion under §2 ("results not yet in
  this report's data").
- **Holding-relevant news** (`holding_news.py` + `config/holding_news_keywords.yml`) —
  recalls window news per moved holding by ticker/alias after anomaly
  detection (fixes macro-theme-only misses); top-3 unmatched anomalies get a
  targeted Tavily search bounded by remaining daily budget. Holdings-derived,
  so this runs AFTER Pass 1 / feeds ONLY Pass 2 (isolation preserved).
- **Data window wording** — footer states the real price cutoff (session-close
  snapshots only, no intraday) and flags `[!] FX rate is stale` when FX trails
  the window by >1 day.
- **Quiet-day suppression** — a short manual re-run (`session_node="manual"`,
  <2h span, 0 news, 0 anomalies) suppresses the heartbeat email; scheduled
  `after_close` quiet windows still email it.


### Report email HTML rendering (issue #24/#117, #118/#119 deferred)

`email_sender.py`'s `_render_html`/`_inline_body_styles` produce the actual
sent HTML — `<head><style>` alone is not load-bearing (Outlook's Word engine
does not reliably apply it), so every client-critical rule is duplicated
inline via BeautifulSoup.

- **Single source of truth**: `_TAG_STYLES: dict[str, str]` (per-tag CSS) is
  used BOTH to stamp inline `style="..."` attributes on every
  markdown-rendered tag AND, via `_build_head_style_rules()`, to generate the
  `<head><style>` block's per-tag rules. This replaced two hand-duplicated
  CSS strings that had already silently drifted (PR #117 Grok review) —
  editing `_TAG_STYLES` is now the only place to change a tag's styling.
- **Bulletproof wrapper**: an outer `width="100%"` table centers an inner
  `width="720"` table (`style="width:720px;max-width:720px;"`) — not a
  `div.wrapper` + CSS `max-width`, which Outlook does not reliably center.
  **`max-width:720px` here is intentional, not a bug** — see the #119
  deferral below before "fixing" it to `max-width:100%`.
- **Zebra striping** (`_stripe_rows`) paints `background-color` (appended
  after the cell's base style, not prepended — CSS last-declaration-wins, so
  append order guarantees the zebra fill can't be silently overridden) plus
  a `bgcolor` attribute on each even row's `td`/`th` cells — not the `<tr>`
  (Outlook often ignores row-level `background`) and not `tr:nth-child(even)`
  (kept in the `<style>` block only as a harmless enhancement for clients
  that honor it). Falls back to striping a table's direct `<tr>` children
  when no `thead`/`tbody` wrapper is present (markdown-it always emits one
  today, but `_inline_body_styles` doesn't assume it).
- **`_render_html` uses `str.replace("__REPORT_BODY__", body)`, not
  `.format(body=...)`** — the generated `<style>` block now contains literal
  CSS braces from `_TAG_STYLES`, which `.format()` would misparse as format
  fields.
- **Verified scope: Gmail (web + app) and Apple Mail only** — Outlook was
  explicitly deprioritized by the product owner ("那么多客户端，我不打算照顾所有邮件客户端"),
  confirmed via two real sends through Resend inspected on real devices.
- **#118 (table-layout:fixed for consistent column widths) and #119
  (wrapper `max-width:100%` for mobile shrink) were both implemented, then
  reverted in the same PR** — #119 was tested via a real send and did not
  fix the Apple Mail rendering problem it targeted (still clipped/broken);
  #118 was reverted alongside it rather than continuing to iterate blind on
  an undiagnosed regression. Both issues are reopened and left as deferred
  backlog, not resolved — do not assume `_TAG_STYLES["table"]` should have
  `table-layout:fixed` or that the wrapper's `max-width` should be `100%`
  without re-diagnosing from scratch first.
- **Review provenance**: two rounds of independent code review (blacktomb42)
  on PR #117 — round 1 found 1 real bug (zebra on `<tr>` instead of cells) +
  3 suggestions/nits, round 2 (after fixes) found 0 bugs + 2 suggestions/2
  nits, all verified against actual code and fixed before merge.


