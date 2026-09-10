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
  the window by >4 calendar days (issue #299 — a weekend/holiday gap of up
  to 4 days is normal; mirrors `portfolio_calculator._PRICE_STALE_DAYS`).
- **Quiet-day suppression** — a short manual re-run (`session_node="manual"`,
  <2h span, 0 news, 0 anomalies) suppresses the heartbeat email; scheduled
  `after_close` quiet windows still email it.


### Report email payload (issue #257)

`send_report_email` posts both `html` and `text` to Resend (the `text`
field is a first-class body parameter on `POST /emails`, confirmed
against Resend's send-email API docs) plus a `headers` object carrying
`List-Unsubscribe` and `List-Unsubscribe-Post`. The plain-text part is
`report.report_md` (already includes the single-language disclaimer
footer, since issue #350 item 3 — see "Report footer disclaimer" below)
plus a short unsubscribe URL line. The HTML footer is the same URL,
rendered as a markdown link through `_render_html`. See
`docs/mechanisms/email-verification.md` (issue #257 section) for the
token/confirm-page mechanism those headers point at.

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



### §3 proportionality check: log-only length-vs-(weight, evidence) (issue #173)

Issue #128's Ring 1 stage B / B1 follow-up asked for an explicit,
code-level check: "if a holding's analysis length is clearly out of step
with its weight-and-evidence-strength pairing, reject and reallocate."
`config/analysis_framework.yml` item 3 (PORTFOLIO SHAPE — WEIGHTED, NOT
FLATTENED) shipped only the prompt-level self-audit half of that ask —
issue #173 adds the code-level CHECK (not yet the reject/reallocate
action — see below).

**Module**: `app/services/section3_proportionality.py`, self-contained
(no DB/Session dependency), exposing:
- `score_evidence_strength(material_text) -> int` (0-5) — keyword/regex
  match count against `EVIDENCE_CATEGORIES`, mirroring
  `analysis_framework.yml` item 2's five structural-evidence categories
  (third-party reliance, sustained capital commitment, insider buying,
  milestone confirmation, competitive-landscape change). Counts
  CATEGORIES matched, never raw term occurrences or price-move magnitude.
- `extract_section3` / `segment_section3_by_holding` — §3 has **no
  per-holding markdown heading** (verified against
  `report_prompts.py`/`report_assembly.py` at implementation time — both
  write §3 as one flowing prose block naming holdings inline; an earlier
  draft of this issue's Design comment assumed a heading convention that
  does not exist, corrected on the issue before implementation per
  AGENTS.md's no-open-questions rule). Segmentation is therefore
  paragraph + identifier-match (product owner decision, 2026-09-10):
  split §3 on blank lines, attribute a paragraph naming exactly one
  holding's identifier/alias terms to that holding in full, exclude a
  paragraph naming multiple holdings from any single holding's length
  (logged as "mixed" for observability, never silently misattributed),
  ignore a paragraph naming no known holding. **Alias terms are NOT only
  `holding_news.load_entity_aliases()`'s table** (PR #423 review,
  blacktomb42 — an earlier version under-matched real §3 prose): they are
  the holding's ticker/fund-code identifier, any configured
  `entity_aliases` row, its own portfolio `name` field, AND that name
  with a trailing legal-entity suffix stripped (`_core_name` in
  `report_generator.py` — "Apple Inc." -> "Apple", "腾讯控股" -> "腾讯").
  `entity_aliases` alone left most holdings unmatched (AAPL has no
  configured row at all), and the full display name alone still missed
  ordinary prose that drops the legal suffix — §3 says "Apple", never
  "Apple Inc.". `load_entity_aliases()` remains in the mix as a
  supplementary source (the same table `cross_name_intel` uses to ask
  "does this prose NAME an identifier" — narrower than
  `load_holding_keywords()`'s broad recall terms, which would false-match
  theme words like "gold"), not the sole source.
- `expected_length_range(weight, evidence_score) -> (min_chars, max_chars)`
  — first-pass heuristic constants, explicitly NOT a calibrated
  enforcement threshold; meant to be tuned against real report data once
  the log-only signal accumulates. `min_chars` has **no unconditional
  floor** (PR #423 review: an earlier version added a flat 80-char floor
  regardless of weight/evidence, so a holding with negligible weight and
  no evidence that §3 legitimately never mentions — the correct default
  per `analysis_framework.yml` item 5 — still warned on every report;
  `min_chars` now scales purely from weight/evidence, `math.floor`'d to 0
  at the low end, so a genuinely immaterial, unevidenced absence is
  in-range rather than noise). `max_chars` keeps its base — the ceiling
  problem is unrelated to this fix.
- `HoldingCheckInput.weight` is an **explicit dataclass field**, never
  read internally off a holding's real position (issue #173 Design item
  4) — this is the interface issue #421's watched/zero-holding entries
  are meant to feed a config-driven target weight into, at the call site
  below, not inside the checker itself.
- `check_section3_proportionality(report_id, full_body_markdown, holdings)`
  — logs one `WARNING` per out-of-range holding (identifier, weight,
  evidence score, actual length, expected range) plus one `INFO` line for
  mixed-paragraph count. Returns an int (mismatch count) for
  tests/metrics only — callers must not branch on it.

**Wiring** (`report_generator.py`): a single integration point,
`_render_full_md` (already the one function both the Pass 2 and
assembly-generation shapes, plus `regenerate_report`'s render/analyze
modes, converge through — Requirements item 3's "both shapes" falls out
of this for free). New optional params `report_id`/`holding_news`
default to `None`; the check only runs when both are supplied. The
quiet-day canned-body path passes neither (nothing to check — no real
per-holding analysis exists on a quiet day). `_build_holding_check_inputs`
assembles each holding's `HoldingCheckInput` from already-gathered,
Pass-2-stage data: real weight via `report_assembly._weight`/`_identifier`,
material text from `ctx.holding_news` (issue #30/R-3's per-holding news
recall) plus that holding's own anomaly record's trigger/theme text — no
new fetch, no LLM call, no search-result text (not available at this
integration point without a larger signature refactor; out of this
issue's scope — see the issue's Exclusion). The whole check call is
wrapped in `try/except Exception` — a bug in the check must never break
report rendering (log-only invariant).

**Invariants verified**: `check_section3_proportionality` never mutates
`full_body_markdown`, has no return path back into `report.status`/email,
and runs independent of `_scan_forbidden_output` (no shared state,
separate call). Two-pass isolation untouched — nothing here reads or
writes `_build_pass1_prompt`; regression tests
`test_pass1_prompt_excludes_holdings_derived_anomalies` and
`test_generate_report_pass1_call_has_no_holdings` stay green.

**Not built here** (deliberately, per Design item 3): re-prompt,
truncate, or any other enforcement of the check's verdict. This is the
observability step; escalating to enforcement is a future decision once
the log data from real reports says whether it's warranted.


### Report footer disclaimer: single-language, not always bilingual (issue #350 item 3)

`report_sections._build_footer` unconditionally emitted both English and
zh-Hans for every report — `footer_header`/`data_sources_label`/
`disclaimer_label`/`disclaimer`, all doubled — regardless of the report's
own language. This predates the per-user `output_lang` mechanism (issue
#308) that now drives the rest of the report body's language per
recipient; the footer had simply never been revisited since. `_build_footer`
now takes an `output_lang: str = "en"` parameter and renders only that
locale's copy, mapped through the existing `locale_for_output_lang`
helper (`en`/`zh` -> `en`/`zh-Hans`). Both call sites thread the real
value through: `report_generator.py`'s full-report path (`output_lang`
already in scope) and `email_sender.py`'s portfolio-overview email (issue
#202 — now passes the recipient's own `users.locale` instead of relying
on the disclaimer being "locale-independent by design", the assumption
this issue removes). Does not touch the disclaimer's content or its
template-layer status — only which language renders.
