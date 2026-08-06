# Portfonia — Agent Guidelines

AI-facing guidance for agent tooling working in this repository.
Last updated: 2026-08-05

## Where to find current state

This file holds **conventions and mechanisms**, not a project status board.

- **Open bugs, requests, and technical debt**: GitHub issues, `debt` /
  `bug` / `enhancement` labels (see "Issue Tracking" below — everything new
  gets an issue first).
- **Ring stage, recent session summaries, running progress**: Obsidian
  `Hermes/Portfonia/` project log.
- **Build/test status, HEAD commit**: `git log`, `pytest -q`, `mypy .` —
  always run these rather than trusting a written-down snapshot.

## System conventions (current behavior, not status)

| Item | Value |
|------|-------|
| LLM model | OpenRouter (provider=DigitalOcean,Venice), `data_collection=deny` on every call. **PRIMARY (Pass 2 analysis) = `deepseek/deepseek-v4-pro`**; **Pass 1 search + translation render = `deepseek/deepseek-v4-flash`** (LOW_COST). Sonnet/Anthropic models are NOT used here — too expensive (~$0.2/call); if `PRIMARY_LLM_MODEL` ever shows an `anthropic/*` value it is config drift, revert it. Translation calls use a separate provider preference (`_TRANSLATION_PROVIDER_ORDER`) since DigitalOcean+Venice were observed 429-ing on `deepseek-v4-flash` translation; `allow_fallbacks=True` still permits OpenRouter beyond this list if both are unavailable. |
| Infrastructure | Homebrew PostgreSQL@16 + Redis (native, not Docker); `make infra-up` not needed |
| **Dev process restart (MANDATORY after model/migration changes)** | uvicorn, `celery worker`, `celery beat` run with **no `--reload`** and load the ORM model at process start. After ANY change to `app/models/*`, an Alembic migration, or a router/schema change, **kill and restart all three** (`ps aux \| grep -E "uvicorn\|celery"`, `kill <pids>`, then `nohup venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info >> .run/uvicorn.log 2>&1 &` and the two `celery -A app.tasks worker/beat --loglevel=info >> .run/{worker,beat}.log 2>&1 &`). Symptom if skipped: `INSERT`/`UPDATE` against the new column fails `NOT NULL`/constraint mismatch → uncaught `IntegrityError` → bare `500` with no traceback. |
| Output language | reason in EN, render in `OUTPUT_LANG` (Ring 0 default `zh`) via a translation pass with a fixed-term glossary (财经分析报告 / 持仓分析 / 持仓机构; never "智能"); `en` = no-op |
| Report statuses | `success` · `skipped` (quiet day, still emails heartbeat — EXCEPT a short manual quiet window: `session_node="manual"` + <2h span + 0 news + 0 anomalies suppresses the heartbeat as a same-day re-run artifact) · `needs_review` (compliance scan hit, NOT emailed) · `failed` · `in_progress` |
| Report title / email subject | `Portfonia 财经分析报告 — YYYY-MM-DD HH:MM ET` (title timestamp from `period_end`); no "智能"/"Intelligence" wording anywhere. |
| Holdings model | `market` + `broker` are user-declared fields; `position` preserves upload order. **§1 groups by `broker` (持仓机构)** in upload order with per-institution subtotals; cash sits inside its institution, broker-less rows fall into "Other". `position` is populated automatically on confirm. |
| Re-render | `regenerate_report(mode=render\|analyze)` rebuilds from stored `report_inputs` without re-fetching; `POST /reports/{id}/regenerate`. render = token-free, analyze = Pass 2 only. |
| §1 / distribution / §4.1 classification dimension | **`asset_class`** (geography-first taxonomy — see table below), not `sector` or `asset_type`. `sector` (yfinance GICS) is retained ONLY for forward-event holding-relevance mapping (rate-sensitive/consumer sectors for FOMC/CPI events) — never reintroduce it into §1/distribution/§4.1. `by_asset_class` has no "Other" fallback (every `Holding` always has one, default `STOCK`). |
| Tests must mock external notify calls | `send_ops_alert`, `create_bug_report`, `send_report_email` are mocked via an **autouse** fixture in `app/tests/conftest.py` (`_no_external_notifications`) — never rely on individual tests remembering to patch them. A gap here previously sent 42 real "FX rates stale" emails to the admin inbox from three same-day pytest runs (test clock fixed to a historical date that always trips the staleness check against the real current date). |

### Capture layer + incremental reporting (ADR-002)

Full spec in Obsidian: `Hermes/Portfonia/Docs/增量报告与捕获层设计.md`.

A **capture layer** (global, credit-free — RSS + yfinance; persists `news` +
`price_snapshots`, 1yr) runs at market-session nodes and feeds a **report
layer** (per-user, incremental).

- **Capture nodes** via crontab `nowfun` per market: US in ET (DST-aware),
  HK/CN fixed-offset. Nodes: US pre_open/open/close/after_close; HK/CN
  open/close. News captured at every node; catch-up logic lives in the task
  (range fetch + idempotent upsert), no watermark table.
- **Report window** = `[previous report.period_end, now]`; watermark =
  `max(period_end)` over the user's completed reports (deleting a report
  rolls it back; regenerate keeps the stored period). News/anomalies are read
  from the stores via `window_data`, never live RSS or last-two-closes.
- **Cadence:** `generate_incremental_report` fires Mon/Wed/Fri 17:00 ET
  (moved from 16:30 ET on 2026-06-19, widening the gap after the 16:05 ET
  FX capture and 16:00 ET close capture).
- **Anomaly detection** (`detect_window_anomalies`) fires on EITHER trigger:
  `single_day` (one trading day beyond per-class per-day threshold — catches
  a violent session a net move would smooth away) or `cumulative` (baseline→
  latest net move beyond a scaled, capped threshold). Flagged holdings carry
  a session arc (prev close/open-gap/intraday range/close/after-hours) for
  §4.2. Report always says "this report period", never "today".
- Portfolio valuation reads the **latest captured close** from
  `price_snapshots`, falling back to `holding.market_price` only for funds
  (no ticker). FX anomalies are not computed (FX stays daily in `fx_rates`).

### Report content features (Ring0 #1-4 + R-3/R-5/R-6/R-7/R-8)

All numbers are **code-built and stored in `report_inputs`** (deterministic,
re-render-safe); the LLM writes only prose/attribution. Current shape:

- **§4.2 price-anomaly table** — session-arc numbers rendered as a markdown
  table; LLM writes one driver line per holding, restricted to "见§4.2" only
  for holdings actually in the table.
- **Confidence labels** — every causal attribution ends with
  `[Established]/[Probable]/[Speculative]` (never a numeric %); zh glossary
  确定/较可能/推测.
- **§4.4 technical position** (`technical_position.py`) — descriptive OHLCV
  facts only (distance to 50/200-day avg, 52-week range, 20-day vol); TA
  signal vocabulary (support/resistance/golden-cross/支撑位/阻力位/金叉/死叉)
  is forbidden in the body. Needs ~200 captured closes — seed once via
  `python -m app.scripts.backfill_ohlcv`.
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

### Reliability mechanisms (window/dedup/LLM-call correctness)

- Same-day report windows (retry/regenerate within one ET calendar date) use
  a `captured_at > start` fallback instead of the date-range query, since a
  same-day range would otherwise collapse to empty even with today's close
  already captured.
- `period_start`/`period_end` are computed once on first attempt and stored
  on the report row; retries reuse the stored window rather than recomputing
  (recomputing made retried content non-deterministic).
- Pass 2 completeness guard: missing `## §3`/`## §4` markers or body
  <2000 chars raises `RuntimeError` so Celery retries instead of persisting a
  silently-truncated `status=success` report.
- `_call_llm` logs model/finish_reason/tokens/cost on every call and warns on
  non-`stop` finish; `LLMEmptyResponseError` on empty `choices` with bounded
  429 backoff-retry. `pin_provider=False` (used only for translation) lets
  OpenRouter route freely instead of restricting to the pinned provider order.
- Resend `Idempotency-Key` is content-addressed
  (`report-{id}-{sha256(html)[:16]}`) — a regenerated report with different
  content gets a different key, avoiding a 409 on corrected resends.
- **`session_node`** (migration `b8c9d0e1f2a3`) identifies WHICH TRIGGER
  produced a report (`"manual"` / `"after_close"` / `"legacy"`), part of the
  reports unique constraint. Set by the caller at generation time, never
  derived from wall-clock at lookup. `user_watermark()` reads `max(period_end)`
  across all `session_node` values for a `report_type`, so a same-day manual
  run and the scheduled after-close run produce non-overlapping windows in
  two separate rows, both emailed independently.

### Compliance + ops alerting (current state)

- `_FORBIDDEN_OUTPUT_PATTERNS` (single source of truth:
  `app/compliance/forbidden_vocab.py`) targets only direct advisory/action
  vocabulary (止损/强烈买入/目标价/投资建议 + EN equivalents). Descriptive
  TA-observation terms (support/resistance/阻力位 etc.) are explicitly
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
- Which holdings map to which class: `_TICKER_ASSET_CLASS` in `app/services/holding_parser.py`.

`ticker_themes` table maps ticker/fund_code → theme for multi-holding
aggregation (e.g. QQQM + 019547 both `nasdaq_100`). Seeded themes:
`nasdaq_100`, `sp500`, `gold`, `japan_equity`, `tbill`.

Fund holdings (fund_code only, no ticker) participate in anomaly detection
via 天天基金 historical NAV: `fund_nav_fetcher.fetch_nav_history()` (lsjz API)
→ `price_capture.capture_fund_navs()` upserts into `price_snapshots` keyed by
fund_code. Beat task `capture_fund_navs_task` runs 20:00 CST Mon-Fri (NAV
publishes same evening after A-share close). `detect_window_anomalies`
identifier fallback chain: `h.ticker or h.fund_code`.

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
overrides are a Ring 1 decision, documented in `产品概念设计文档.md`, not
built yet.

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

### Known-fixed bugs worth remembering (regression notes)

- **Fund NAV lookup**: `compute_portfolio` must look up price data with
  `captured_closes.get(h.ticker or h.fund_code or "")` — fund code-only
  holdings have no `ticker`, and `capture_fund_navs` stores NAV in
  `price_snapshots` keyed by `fund_code`. A ticker-only lookup silently drops
  every fund holding into `stale_tickers` and out of the portfolio. (issue #1)
- **Sector backfill on re-upload**: `confirm_holdings` must call
  `backfill_sectors()` after commit — re-uploading holdings clears all rows,
  and `sector` is otherwise only populated by `POST /portfolio/refresh`.
- **Next.js Turbopack + multipart**: Turbopack's `rewrites()` fails on
  `multipart/form-data` POST (ECONNRESET at proxy). Upload routes need a real
  Next.js API Route (`route.ts`) that manually forwards to the backend.

## Architecture

| Layer | Choice |
|-------|--------|
| Frontend | Next.js + shadcn/ui |
| Backend | Python FastAPI |
| Database | PostgreSQL, self-hosted in Docker on the production VPS (not Supabase-managed — decided 2026-08-05 to cut hosting complexity). Supabase is used for **Auth only**. |
| Task queue | Celery + Redis |
| LLM | Pluggable (Claude / DeepSeek / etc.) — keep provider-swappable |
| Local dev | Homebrew PostgreSQL 16 + Redis (native); Colima for Hermes gateway only |
| Production | OCI Ampere A1 Flex, `instance-portfonia-web`, 1 OCPU/6GB (Always Free ceiling — see note below), Ubuntu 24.04 LTS |

### Three-layer deployment flow (MANDATORY)

Full workflow + production server specs: Obsidian `Hermes/Portfonia/开发环境配置.md`.
The one hard rule that governs every action here: code authority is
**local → Git only**. Never edit code on the VPS, never `git commit` on the
VPS, never use the VPS as a sync hub between machines — its only legitimate
local state is `.env` (uploaded via `scp`).

**OCI free-tier ceiling drifts — re-verify before assuming a number.** Oracle
silently cut the Always Free Ampere A1 pool from 4 OCPU/24GB to 2 OCPU/12GB
on 2026-06-15, and the console showed a further-reduced 1 OCPU/6GB ceiling
by 2026-08-05 (what `instance-portfonia-web` is actually provisioned at).
Don't hardcode a spec number from memory — check the OCI console or `oci
compute instance list` before planning capacity.

**SSH stays open to `0.0.0.0/0` on `instance-portfonia-web`, guarded by
fail2ban only** (`maxretry=10`, `findtime=10m`, `bantime=10m` — relaxed from
defaults after a prior fail2ban lockout on the Stalwart mail server cost 3
hours to recover from serial console). No source-IP restriction: the dev
machine has no fixed IP, and an agent session's own egress IP isn't stable
across runs either. If a future session gets banned mid-task, the ban
self-clears in 10 minutes — don't burn time trying to route around it via
OCI serial console unless the task can't wait.

**This project's VPS SSH connectivity is unreliable — the connection can
drop mid-command with no warning** (confirmed 2026-08-06: two separate
`docker compose up --build` launches died silently mid-build, one via
`nohup ... & disown` on the remote side, one via keeping the SSH session
itself alive locally with `run_in_background` — neither survives an actual
network drop, because both still depend on the TCP/SSH connection staying
up long enough to hand off). **For any remote command expected to run
longer than a few seconds, use `systemd-run` on the VPS** so the command
runs as a transient unit fully independent of the SSH session:

```bash
ssh ubuntu@170.9.11.11 "sudo systemd-run --unit=portfonia-deploy --working-directory=/home/ubuntu/Portfonia -- docker compose up -d --build"
# reconnect any time after, even following a dropped connection, to check on it:
ssh ubuntu@170.9.11.11 "systemctl status portfonia-deploy; sudo journalctl -u portfonia-deploy --no-pager"
```

Do not trust a `nohup`/`disown`/backgrounded-SSH exit code as proof a long
remote command finished on this VPS — verify by checking the actual
resulting state (containers running, files present), not just the shell's
reported exit status.

**`instance-20260421-0710` in this same OCI tenancy belongs to a different,
unrelated project — never touch it** (stop/resize/reconfigure/reuse) when
working on Portfonia infra. It sits in its own VCN, isolated from
`instance-portfonia-web`.

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

## Issue Tracking (MANDATORY)

Every new feature/improvement request and every bug — regardless of whether
it's fixed immediately — gets a GitHub issue first, before the fix/feature
work starts. Issues are the project's request/bug ledger; the CLAUDE.md debt
table is for cross-session technical-debt reminders only, not a substitute.

- **Blocking / fix-now**: open issue → fix/implement → comment with commit
  hash + approach + verification → close.
- **Deferred**: open issue → leave in backlog → comment + close when later
  addressed.

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

Full product-scope decisions (what we deliberately don't build, and why)
live in Obsidian `Hermes/Portfonia/产品概念设计文档.md` §1 + appendix — not
here, to keep this file to AI-actionable conventions rather than product
ideation. Quick check before any new feature: trade execution, tax/P&L
tracking, options/derivatives, price-only threshold alerts, social/sharing
features, and stock-pick-style recommendations are all explicitly excluded.

## When Principles Conflict

- **Compliance > everything**. If a feature can't be shipped without crossing
  the layer-3 boundary, the feature does not ship.
- **UX > YAGNI** for user-facing surfaces. If users need it, it's not
  speculative.
- **KISS applies to code AND user journey** — fewer steps, fewer options,
  fewer modes by default.
- **Reversibility check before destructive actions** (DB migrations dropping
  columns, `rm -rf`, force pushes). Confirm with the user before executing.
