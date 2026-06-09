# Portfonia — Agent Guidelines

AI-facing guidance for agent tooling working in this repository.
Last updated: 2026-06-07

## Current Development State (update this section when resuming)

| Item | Value |
|------|-------|
| Ring stage | **Ring 0** (local dev machine, single user, no cloud) |
| `main` HEAD | `484bcf8` feat(reports): §2.5 US forward calendar (FRED macro + FOMC + earnings) |
| Stages complete | A B C D E F1 F2 F3 G H + June-7 report-review fixes + **ADR-002 incremental reporting + capture layer** + **Ring0 report enhancements #1–#4** (§4.2 anomaly table, confidence labels, §4.4 technical position, §2.5 forward calendar) |
| Next stage | **I** — 稳定运行验证；报告现为增量（M/W/F 16:30 ET），需先让捕获层攒数据 |
| Backend quality | ruff OK · mypy OK (74 files) · pytest **272 passed** |
| Frontend quality | tsc OK · eslint OK · next build OK |
| LLM model | OpenRouter (provider=DigitalOcean,Venice), `data_collection=deny` on every call. **PRIMARY (Pass 2 analysis) = `deepseek/deepseek-v4-pro`**; **Pass 1 search + translation render = `deepseek/deepseek-v4-flash`** (LOW_COST). Sonnet/Anthropic models are NOT used here — too expensive (~$0.2/call); if `PRIMARY_LLM_MODEL` ever shows an `anthropic/*` value it is config drift, revert it. |
| Infrastructure | Homebrew PostgreSQL@16 + Redis (native, not Docker); `make infra-up` not needed |
| Prompt version | `f2-v4` (adds: §4.2 LLM writes driver-only one-liners — numeric arc is a code-built table; evidence confidence labels `[Established]/[Probable]/[Speculative]` on every causal attribution; forward-event no-forecast rule. f2-v3 = window-anchored §2/§3/§4 depth + anomaly session-arc + NO inline markers; f2-v2 = Pass 1 public-only) |
| Disclaimer version | `f3-bilingual-v1` |
| Output language | reason in EN, render in `OUTPUT_LANG` (Ring 0 default `zh`) via a translation pass with a fixed-term glossary (财经分析报告 / 持仓分析 / 持仓机构; never "智能"); `en` = no-op |
| Report statuses | `success` · `skipped` (quiet day, still emails heartbeat) · `needs_review` (compliance scan hit, NOT emailed) · `failed` · `in_progress` |
| Report title / email subject | `Portfonia 财经分析报告 — YYYY-MM-DD HH:MM ET` (title timestamp from `period_end`); no "智能"/"Intelligence" wording anywhere. |
| Holdings model | `market` + `broker` are user-declared fields; `position` preserves upload order. **§1 groups by `broker` (持仓机构)** in upload order with per-institution subtotals; cash sits inside its institution, broker-less rows fall into "Other". Distributions (by market/currency/asset type) unchanged. NOTE: `position` is currently NULL on existing rows (parser does not set it) → within-group order falls back to DB order until the parser populates it. |
| Re-render | `regenerate_report(mode=render\|analyze)` rebuilds from stored `report_inputs` without re-fetching; `POST /reports/{id}/regenerate`. render = token-free, analyze = Pass 2 only. |

### Known technical debt (carry forward until resolved)

| ID | Description | Resolve at |
|----|-------------|-----------|
| F-DEBT-1 | `by_sector` "Other" conflates ETFs with unclassified equities | Ring 1+ |
| D-DEBT-1 | `backfill_sectors` serial O(n) `yf.Ticker().info` calls | Ring 1+ |
| D-DEBT-2 | `compute_portfolio` has no `user_id` filter (loads all holdings) | MVP |
| D-DEBT-3 | 天天基金 OCI reachability unverified | Ring 1 deploy |
| D-DEBT-4 | Price staleness: `stale_tickers` only catches NULL price, not stale-dated prices | TBD |
| G-DEBT-1 | `send_report_email` returns True even if `commit()` of `email_sent_at` fails; Resend Idempotency-Key prevents duplicate delivery but state is unpersisted. Persist a provider message id / use a conditional update. | Ring 1+ |
| G-DEBT-2 | Concurrent-send dedup is best-effort (in-memory `email_sent_at` check + Resend Idempotency-Key), not a DB lock. Add a row lock / conditional update if multi-trigger paths appear. | Ring 1+ |
| G-DEBT-3 | Email HTML uses a `<style>` block (`nth-child`, `max-width`, `border-radius`); not robust in Outlook/Gmail. Inline critical styles / table-based wrapper before real external recipients. | Ring 1+ |
| A-DEBT-1 | No DB domain constraints: `pricing_mode`/`asset_type`/`currency` are free `Text` (no CHECK/enum); `shares`/`current_value` have no `>= 0`. App layer + `ParsedRow` Literals are the only guard. Add CHECK constraints via migration **after** auditing existing dev rows (a CHECK that fails on legacy data blocks upgrade). | Ring 1 |
| A-DEBT-2 | Test suite drops+creates+migrates a fresh DB per `db_session` test (correct for drift detection, but slow). Move to session-scoped migrated DB + per-test SAVEPOINT rollback. | Ring 1 |
| A-DEBT-3 | `core/database.py` builds module-level `engine`/`SessionLocal` bound to the dev DB at import. Test isolation relies on every test overriding `get_session` or patching `SessionLocal` (discipline, not structure). | Ring 1 |
| A-DEBT-4 | `ParsedRow` uses `float` for `shares`/`avg_cost`/`current_value`, bridged to `Decimal` via `Decimal(str(x))` at confirm. Bridge is adequate but inconsistent with the Decimal-everywhere model. | TBD |
| A-DEBT-5 | `/holdings/upload` has no request body-size cap (`await file.read()` loads fully into memory). Local Ring 0 low risk; add a limit before any exposed deployment. | Ring 1 |
| H-DEBT-1 | **Report idempotency is keyed on `(user_id, report_date, report_type)` — a calendar-day grain.** This blocks more than one report of a type per day: a re-run short-circuits to the existing row. When pre-open / after-close (morning/night) reports are added, multiple same-day runs of the same cadence would be (wrongly) treated as duplicates and silently dropped, losing intel. Re-key dedup on the report **window / session node** (e.g. `(user_id, report_type, session_node)` or the `period_end` slot), not the date. Until fixed, a same-day re-run must reset the existing row's status first (and the watermark then rolls back to the prior report / bootstrap). | before morning/night reports ship |

### Incremental reporting + capture layer — DONE (ADR-002)

Full spec in Obsidian: `Hermes/Portfonia/Docs/增量报告与捕获层设计.md` (resolved #3
report-window-vs-cadence and #4 observation cadence from the 2026-06-07 review).

Shipped shape: a **capture layer** (global, credit-free — RSS + yfinance;
persists `news` + `price_snapshots`, 1yr) runs at market-session nodes and feeds
a **report layer** (per-user, incremental). Agent-facing essentials:

- **Capture nodes** scheduled via crontab `nowfun` per market: US in ET
  (DST-aware), HK/CN in their fixed-offset zones. Nodes: US pre_open/open/close/
  after_close; HK/CN open/close. News captured at every node. Catch-up is in the
  task (OHLCV range fetch / 48h news + idempotent upsert) — no watermark table.
  Tasks: `capture_news_task`, `capture_prices_task(market, node)`.
- **Report window** = `[previous report.period_end, now]`; watermark =
  `max(period_end)` over the user's completed reports (derived → deleting a
  report rolls it back; regenerate keeps the stored period). Cold-start baseline
  = `2026-06-01 16:00 ET` (`window_data.BOOTSTRAP_WATERMARK`).
- News + anomalies read from the stores via `window_data` (NOT live RSS / last-
  two-closes). New positions (no baseline) skipped.
- **Cadence:** `generate_incremental_report` (report_type=`incremental`) fires
  Mon/Wed/Fri 16:30 ET. Migrations: `e5f6a7b8c9d0` (capture tables),
  `f6a7b8c9d0e1` (report period columns).

Anomaly detection (`detect_window_anomalies`) fires on **either** of two triggers
(2026-06-08 rework, fixes the "no anomalies on a volatile week" miss):
- **single_day** — any one trading day in the window moved beyond the per-day
  threshold (stock 3%, etf 2%). Catches a violent session the endpoint-to-
  endpoint net move would smooth away.
- **cumulative** — baseline-close → latest-close net move beyond the scaled
  threshold (per-day × trading-days, capped at 10%; `_window_threshold`).
Every flagged holding also carries the most-recent-trading-day **session arc**
(prev close, open+gap, intraday high/low, close, after-hours) so §4.2 can state
the comparison basis and describe how the day ran. The report states the
trading-day count and refers to "this report period", never "today".

Portfolio **valuation reads the latest captured close** from `price_snapshots`
(`_latest_captured_closes`), falling back to `holding.market_price` only for
funds (no ticker). This keeps §1 valuation and the anomaly baseline on one price
series. FX window anomalies are still not computed (FX stays daily in `fx_rates`).

### Ring0 report enhancements #1–#4 — DONE

Four first-user-feedback features, all inside the Layer-3 boundary. The pattern
throughout: **numbers are code-built and stored in `report_inputs`** (deterministic,
token-free, re-render-safe — `regenerate_report(mode=render)` reproduces them with
no DB read); the **LLM writes only prose/attribution**.

- **#3 §4.2 price-anomaly table** (`_build_section42_table` + `_inject_section42_table`):
  the session-arc numbers become a markdown table inserted under the LLM's
  `### 4.2 Price anomalies` heading; the LLM writes only a one-line driver per holding.
- **#2 confidence labels**: every causal attribution (§3, §4.2) ends with an
  evidence-ordinal `[Established]/[Probable]/[Speculative]` (never a numeric %);
  large unexplained moves are kept and labelled `[Speculative]`, not dropped. zh
  glossary maps the labels (确定/较可能/推测).
- **#4 §4.4 technical position** (`technical_position.py` + `_build_section44_technical`):
  descriptive price structure from captured OHLCV — distance to 50/200-day average,
  52-week range position, 20-day annualized volatility. Pure facts, NO TA-signal
  vocabulary (new forbidden patterns: support/resistance level, golden/death cross,
  breakout, 支撑位/阻力位/金叉/死叉). Needs ~200 captured closes for the long windows →
  run **`python -m app.scripts.backfill_ohlcv`** once (idempotent; reuses
  `capture_prices(close, lookback_days=420)`) to seed a year of closes.
- **#1 §2.5 forward calendar** (`forward_events.py`, `forward_events` table, migration
  `a7b8c9d0e1f2`): scheduled US events ~10 days out, each mapped (in code) to exposed
  holdings. Sources: FRED `release/dates` for a curated release set (CPI/PPI/NFP/
  Retail/PCE/GDP/UMich; needs `FRED_API_KEY`, optional — macro skipped if unset),
  **hardcoded FOMC statement dates** (verified from federalreserve.gov; FRED's FOMC
  release has no forward schedule — VERIFY ANNUALLY), and earnings via yfinance
  `Ticker.calendar`. Calendar facts only — `_PASS2_SYSTEM` bars forecasting event
  outcomes. An RSS-derived delay caveat fires when window news mentions a funding
  lapse (BLS/BEA dates may slip). **China forward intel is out of scope.** Captured
  by `capture_forward_events_task` (daily 08:00 ET, Mon–Fri, 14-day fetch horizon).

## Language Policy (MANDATORY)

- **All repository content is English**: code, identifiers, comments, commit
  messages, PR descriptions, issue text, README, `docs/`, ADRs, tests.
- **In-product strings are i18n-keyed** and shipped through the translation
  layer, never hardcoded in any single language. Supported runtime UI and
  report languages: English and Simplified Chinese (extensible).
- Translation resources live under a dedicated locales directory and are the
  only place where non-English text legitimately appears in the repo.

## Product Boundary (NEVER VIOLATE)

Portfonia is an **intelligence service**, not an advisory service.

### Three-layer output rule

AI-generated content stops at layer 3. Layer 4 is a hard prohibition.

```
Layer 1  What happened                       (pure fact)
Layer 2  How it relates to your holdings     (contextual mapping, no judgment)
Layer 3  Signals worth watching              (point to observation, not action)
─────────────────────────────────────────────────────────────────
Layer 4  What you should do                  (FORBIDDEN — never emit)
```

### Forbidden vocabulary in any AI-generated output

`recommend`, `should`, `buy`, `sell`, `hold`, `reduce`, `increase`, `exit`,
`stop-loss`, `target price`, `will rise/fall to`, `entry point`, `oversold`,
`overbought`, `strong buy`, `bullish/bearish rating` — and their equivalents
in any other language.

### Compliance scaffolding

- Disclaimer text is injected at the **template layer**, not by the model.
  Every report has fixed header + footer disclaimers (EN + zh-CN). AI fills
  only the body region.
- Prompt-level hard constraints (the layer-3 rule + vocabulary blacklist)
  are part of the system prompt for every report and Q&A flow. Do not move
  these constraints to user-tunable prompts.
- **Output-side backstop**: prompt instructions are not a guarantee, so the
  generated body is scanned post-generation (`_scan_forbidden_output`) for
  high-precision advisory phrases. A hit sets the report status to
  `needs_review` and **suppresses email** — content is preserved for
  inspection, never delivered. The scan covers the LLM body only, never the
  template footer (whose disclaimer legitimately contains "buy/sell").
- **Single footer disclaimer, no inline markers** (2026-06-08): the compliance
  base is the one bilingual disclaimer in the footer. The body carries NO
  per-sentence `[For information only…]` suffix and NO bracketed provenance tags
  (`[行情]`/`[新闻]`/`[分析]`/`[S#]`). The system prompt forbids the model from
  emitting them, and `_strip_markers` removes any that slip through. The scan
  backstop above does not depend on the suffix.

## Architecture

| Layer | Choice |
|-------|--------|
| Frontend | Next.js + shadcn/ui |
| Backend | Python FastAPI |
| Database | PostgreSQL (Supabase managed, includes Auth) |
| Task queue | Celery + Redis |
| LLM | Pluggable (Claude / DeepSeek / etc.) — keep provider-swappable |
| Local dev | Homebrew PostgreSQL 16 + Redis (native); Colima for Hermes gateway only |
| Production | OCI Ampere A1 (Ubuntu 24.04 LTS) |

### Three-layer deployment flow (MANDATORY)

```
Local (~/Portfonia)   →   GitHub   →   VPS (git pull && docker-compose up -d)
   write code             transport      run only
```

- Code authority lives in **local → Git**. The VPS is never an editor.
- The only legitimate VPS-side state outside Git is `.env` (uploaded via `scp`).
- Never edit code on the VPS, never `git commit` on the VPS, never use the VPS
  as a sync hub between machines.

## Secrets and Configuration

- `.env` files are **never** committed. Enforce via `.gitignore` from day one.
- API keys (Claude, Resend, market-data providers) are loaded from `.env` only.
  Never hardcode, never log, never echo to stdout in error paths.
- For test code: never read or write the developer's real `~/.config/...`
  directories. Honor a project-scoped env var (e.g. `PORTFONIA_HOME`) and
  default tests to a temp dir. Direct use of `os.path.expanduser("~")` in
  code that tests will exercise is a bug.

## Data Handling

- User holdings are sensitive. Encrypt at rest. **Never** include raw user
  holdings in training data, LLM fine-tuning datasets, or third-party logs.
- When sending holdings to an external LLM, scope the payload to what the
  current report needs. Do not attach the full portfolio history "just in case".
- **Two-pass isolation (enforced):** Pass 1 (search-query generation, low-cost
  model) must carry only public data — macro themes + news headlines.
  Holdings-derived data, including **price anomalies** (their name/ticker
  reveals a position), belongs only in Pass 2. Regression locked by
  `test_pass1_prompt_excludes_holdings_derived_anomalies` and
  `test_generate_report_pass1_call_has_no_holdings`. Do not reintroduce
  holdings into `_build_pass1_prompt`.
- **`data_collection=deny` is applied to every LLM call** (not just
  holdings-bearing ones) as defense in depth: even if holdings leak into Pass 1
  in the future, the call still cannot route to training providers.
- Market data: cache same-day, same-symbol queries. yfinance is the default
  source; treat rate limits as a real constraint when adding new query paths.
- FX rates: pull once per day into the FX table; all valuation reads from that
  table. Do not call the FX source from request paths.

## Quality Gates (run BEFORE pushing)

Order matters because `validate` checks formatting non-destructively.

```bash
# Backend (FastAPI / Python)
ruff format .            # 1. fix formatting
ruff check --fix .       # 2. fix lints
mypy .                   # 3. types
pytest -q                # 4. tests

# Frontend (Next.js)
bun run format           # 1. prettier write
bun run lint:fix         # 2. eslint --fix
bun run typecheck        # 3. tsc --noEmit
bun run test             # 4. tests
```

Final gates (CI also enforces):

- Type check passes (mypy strict, tsc strict).
- Lint passes with zero warnings.
- Format check passes (non-mutating).
- All tests pass.
- No `any` / `Any`, no non-null assertions, no unused exports.

## CI-First Protocol (MANDATORY)

> **Ring 0 reality:** there is no CI yet and no PRs — work is committed directly
> to `main` (solo) with the local quality gate run before every commit, and
> pushed the same day for off-site backup. The protocol below is the **Ring 1+
> target** that activates when CI exists / a second contributor joins. Until
> then "CI green" means "local gate green".

A task is NOT complete until CI is green.

After every `git push`:

1. Immediately run `gh pr checks --watch` (or `gh run watch`) and block until
   all checks finish.
2. **Green** → task may proceed.
3. **Red** → pull failing logs with `gh run view --log-failed`, fix the root
   cause locally (never retry blindly), commit, push again, re-watch.

Do not declare a task done, close a session, or move to the next task while
CI is red or still running. Leaving a PR red and moving on is the primary
failure mode this protocol exists to prevent.

## Branching

> **Ring 0 reality:** solo work commits directly to `main`; the branch/PR model
> below is the **Ring 1+ target** (adopt when a second contributor joins or VPS
> deploys begin).

```
main (production) ← dev (integration) ← feat/* | fix/* | docs/*
                                          ↑
                                          hotfix/* (only emergencies, from main)
```

- Never commit directly to `main` or `dev`.
- Feature branches start from `dev`. Hotfix branches start from `main`.
- `dev → main` promotion PRs must use `feat:` or `fix:` (a `chore:` title
  will not trigger a release).
- Delete branches after merge.

## Conventional Commits (MANDATORY)

Format: `<type>(<scope>): <description>`

| Type | Version bump | Use for |
|------|--------------|---------|
| `feat:` | MINOR | new feature |
| `fix:` | PATCH | bug fix |
| `perf:` | PATCH | performance |
| `feat!:` | MAJOR | breaking change |
| `docs:`, `style:`, `refactor:`, `test:`, `chore:`, `ci:`, `build:` | none | non-release |

Examples:
- `feat(reports): add cross-market FX-normalized valuation`
- `fix(ingest): handle yfinance rate-limit on HK tickers`
- `docs: clarify layer-3 boundary in prompt template`

## Releases

Releases are automated. **Never** bump versions or create tags by hand.
Let CI handle versioning, changelog, tag, and publish.

## Code Standards

- **Python**: 3.11+, FastAPI, Pydantic v2, type hints required, `ruff` for
  lint + format, `mypy --strict` for types. No `Any` without justification.
- **TypeScript**: strict mode on. No `any`, no `!` non-null assertions, no
  unused locals/params (prefix with `_` only if intentionally unused).
- **No emojis in CLI output or server logs.** Use ASCII markers (`[OK]`,
  `[!]`, `[ERR]`, `[i]`). Emojis are fine in product UI copy and reports.
- Respect `NO_COLOR` for any terminal output.
- Boundary validation: validate at system boundaries (HTTP handlers, file
  loaders, external API responses). Do not re-validate inside internal
  function chains — trust your types.

## Tests

- Unit tests live next to the code they cover.
- Integration tests hit a real Postgres (docker-compose service), not a mock.
  The whole point is to catch schema/migration drift.
- LLM prompt regressions: keep a small fixture of "input portfolio + expected
  shape of output" so prompt edits don't silently violate the layer-3 rule.
- Never let tests touch the developer's real home directory.

## Documentation

- `README.md` — short, user-facing intro, install, run.
- `docs/` — architecture, ADRs, runbook snippets. All English.
- Update docs **in the same PR** as the code change that motivates them.
- API-level changes update `--help` text / OpenAPI schema / route docs in
  the same PR. Code and docs out of sync is a defect.

## Out of Scope (do not let scope creep pull this in)

- Trade execution.
- Tax / capital-gains computation.
- Transaction-log tracking (P&L from buy/sell history).
- Options / futures / derivatives.
- Price-only alerts ("ticker dropped 8%") — every broker app does this; we do
  signal-driven alerts, not threshold alerts.
- Social / sharing features (sensitive data — defer until Phase 2 with
  serious anonymization review).

## When Principles Conflict

- **Compliance > everything**. If a feature can't be shipped without crossing
  the layer-3 boundary, the feature does not ship.
- **UX > YAGNI** for user-facing surfaces. If users need it, it's not
  speculative.
- **KISS applies to code AND user journey** — fewer steps, fewer options,
  fewer modes by default.
- **Reversibility check before destructive actions** (DB migrations dropping
  columns, `rm -rf`, force pushes). Confirm with the user before executing.
