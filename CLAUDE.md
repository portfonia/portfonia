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
| LLM model | OpenRouter, split by call shape (issue #78, 2026-08-06). **Structured/JSON** (holdings parsing, `holding_parser.py`, the only call site requiring schema-compliant output) = `STRUCTURED_LLM_MODEL` (`openai/gpt-5.6-luna` — moved off `google/gemma-4-31b-it` in issue #84, 2026-08-06: the gemma pin to OpenInference's bf16 endpoint was itself the latency bottleneck, 371s worst case on a 30-row holdings file; `gpt-5.6-luna` measured 10.9-13.8s on the same file with 30/30 rows correct on manual audit — one manual run, not yet a systematic eval), `reasoning_effort=none` (`_STRUCTURED_REASONING_EFFORT` in `holding_parser.py` — this model defaults reasoning to "medium", wasted cost/latency for mechanical extraction), open/unpinned provider selection for both of 2 identical attempts (`app/core/llm.py:structured_provider` — no precision-pin concern for this model, unlike gemma's third-party quantized resellers); `data_collection=deny` applies throughout. **Unstructured/free-text** (Pass 1 search-query gen + translation render, `report_generator.py`) = `LOW_COST_LLM_MODEL` (`~deepseek/deepseek-v4-flash-latest` — leading `~` is OpenRouter's "-latest" alias convention), routed via OpenRouter BYOK straight to DeepSeek's own backend (`order=["DeepSeek"]`, module constant `_BYOK_PROVIDER_ORDER`) with `enforce_data_collection=False` — a scoped compliance exception for these two calls only — **and `allow_fallbacks=False` (hard pin, no marketplace fallback)**: since `deny` is off for these calls, an open fallback on DeepSeek unavailability could silently reroute the (holdings-bearing, for translation) payload to a training-permitting provider `deny` would normally have excluded; the call must fail rather than degrade that guarantee (PR #79 review finding). Reasoning/thinking tokens are explicitly disabled (`disable_reasoning=True`) since this alias defaults reasoning on unlike the non-aliased model. **PRIMARY (Pass 2 analysis + regenerate) = `deepseek/deepseek-v4-pro`**, unchanged — provider=DigitalOcean,Venice, `data_collection=deny`, no BYOK. Sonnet/Anthropic models are NOT used here — too expensive (~$0.2/call); if `PRIMARY_LLM_MODEL` ever shows an `anthropic/*` value it is config drift, revert it. |
| Infrastructure | Homebrew PostgreSQL@16 + Redis (native, not Docker); `make infra-up` not needed |
| **Dev process restart (MANDATORY after model/migration changes)** | uvicorn, `celery worker`, `celery beat` run with **no `--reload`** and load the ORM model at process start. After ANY change to `app/models/*`, an Alembic migration, or a router/schema change, **kill and restart all three** (`ps aux \| grep -E "uvicorn\|celery"`, `kill <pids>`, then `nohup venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info >> .run/uvicorn.log 2>&1 &` and the two `celery -A app.tasks worker/beat --loglevel=info >> .run/{worker,beat}.log 2>&1 &`). Symptom if skipped: `INSERT`/`UPDATE` against the new column fails `NOT NULL`/constraint mismatch → uncaught `IntegrityError` → bare `500` with no traceback. |
| Output language | reason in EN, render in `OUTPUT_LANG` (Ring 0 default `zh`) via a translation pass with a fixed-term glossary — locale-keyed, single source of truth in `backend/config/i18n_glossary.yml` (`report_glossary`/`forbidden_renderings`; only `zh-Hans` populated today, schema reserves `zh-Hant`/`fr`/`es` for later); `en` = no-op |
| Report statuses | `success` · `skipped` (quiet day, still emails heartbeat — EXCEPT a short manual quiet window: `session_node="manual"` + <2h span + 0 news + 0 anomalies suppresses the heartbeat as a same-day re-run artifact) · `needs_review` (compliance scan hit, NOT emailed) · `failed` · `in_progress` |
| Report title / email subject | `Portfonia <Financial Analysis Report> — YYYY-MM-DD HH:MM ET` (title timestamp from `period_end`; the zh-Hans render substitutes the `report_glossary` term for "Portfonia Financial Analysis Report" from `i18n_glossary.yml`); no "Intelligence" wording, or its zh-Hans equivalent (`forbidden_renderings` in the same file), anywhere. |
| Holdings model | `market` + `broker` are user-declared fields; `position` preserves upload order. **§1 groups by `broker` (rendered as "Custodian" — zh-Hans term in `i18n_glossary.yml`'s `report_glossary`)** in upload order with per-institution subtotals; cash sits inside its institution, broker-less rows fall into "Other". `position` is populated automatically on confirm. |
| Holdings upload | Async, not a single blocking request — see "Async holdings upload" section below (issue #77/#82/#85). |
| Re-render | `regenerate_report(mode=render\|analyze)` rebuilds from stored `report_inputs` without re-fetching; `POST /reports/{id}/regenerate`. render = token-free, analyze = Pass 2 only. |
| §1 / distribution / §4.1 classification dimension | **`asset_class`** (geography-first taxonomy — see table below), not `sector` or `asset_type`. `sector` (yfinance GICS) is retained ONLY for forward-event holding-relevance mapping (rate-sensitive/consumer sectors for FOMC/CPI events) — never reintroduce it into §1/distribution/§4.1. `by_asset_class` has no "Other" fallback (every `Holding` always has one, default `STOCK`). |
| Tests must mock external notify calls | `send_ops_alert`, `create_bug_report`, `send_report_email` are mocked via an **autouse** fixture in `app/tests/conftest.py` (`_no_external_notifications`) — never rely on individual tests remembering to patch them. A gap here previously sent 42 real "FX rates stale" emails to the admin inbox from three same-day pytest runs (test clock fixed to a historical date that always trips the staleness check against the real current date). |

### Async holdings upload (issue #77/#82/#85)

`POST /holdings/upload` used to run `holding_parser.parse()` synchronously
inside the request — up to 3 sequential LLM attempts that could take
minutes, fragile against any interruption on the long-lived connection in
the meantime (issue #77: confirmed once in production — the backend
completed and returned 200, but the client never saw it because the
connection had already dropped, so `POST /holdings/confirm` was never
called and nothing committed. Not itself a data-loss bug — just a
never-happened commit — but the UX read as "upload failed").

- **Shape**: `POST /holdings/upload` extracts text, writes it onto a new
  `UploadJob` row (`status="pending"`), enqueues `parse_holdings_upload`,
  and returns 202 immediately with the job. `GET
  /holdings/upload/{job_id}` is the poll target — same shape `/upload` used
  to return directly (`preview`/`error`), scoped to the requesting user
  (404 for another user's job). Mirrors the existing `Report.status`
  pattern rather than inventing a new one.
- **`raw_text` never becomes a Celery/Redis broker argument** — the task
  takes `job_id` only. Redis persists queued task payloads until ack under
  `task_acks_late=True` (global setting), so passing the extracted holdings
  text as a task arg would put plaintext portfolio content in the broker
  itself, a new surface the old request-scoped in-memory path never had.
  `raw_text` is cleared the moment the parse attempt is done with it
  (success, failure, or timeout-driven resolution) — never left populated
  once a job leaves `pending`.
- **45s SLA** (`_SLA_SECONDS` in `holdings_tasks.py`, `soft_time_limit=43,
  time_limit=45`): based on `STRUCTURED_LLM_MODEL` real latency
  (`openai/gpt-5.6-luna`, 10.9-13.8s per attempt — issue #84/#86, moved off
  `google/gemma-4-31b-it` whose OpenInference bf16 pin was itself the
  bottleneck, 371s worst case observed).
- **Hard-kill resolution is two layers, not one** (issue #85 — a
  Celery hard `time_limit` sends the worker process SIGKILL, an OS signal
  the task's own `except`/`finally` can never catch): `_UploadJobRequest`
  (a `Task.Request` subclass wired via the task's `Request=` kwarg)
  overrides `on_timeout()` to resolve the job to `failed` from Celery's
  MainProcess the moment the hard kill is detected — verified against a
  real SIGKILL in dev (`time_limit=2`), not just mocked. `Request.terminate()`
  (`task_revoked` signal) is NOT this path — verified against the installed
  `celery==5.6.3` source that a hard `time_limit` kill never emits
  `task_revoked`; that signal only fires from an explicit admin
  `revoke(terminate=True)`, kept as a separate, narrower safety net for
  that case. Backstop: `sweep_stale_upload_jobs` (Celery beat, every 30s)
  marks any row still `pending` past `_SLA_SECONDS + 45` as `failed` —
  catches whatever the immediate hook itself misses (MainProcess restart,
  worker host down). The 45s buffer accounts for queue wait (holdings jobs
  share the default worker pool with capture tasks), not just parse time.
- **Idempotent against Celery redelivery**: `task_acks_late=True` means a
  worker that dies after committing success/failure but before acking gets
  the same message redelivered. The task checks `job.status != "pending"`
  first and returns early for anything already terminal — otherwise a
  redelivered run would see `raw_text` already cleared, misread that as
  "nothing to parse," and overwrite a real success with a false failure.
- **Frontend poll** (`uploadHoldings` in `api.ts`): polls immediately after
  POST (no fixed floor delay), then backs off 500ms→2s, capped at 120s
  total before throwing a clear timeout `ApiError` — a stuck-pending job
  (worker down, broker loss after enqueue) must not spin the UI forever.
- **Upload size cap** (`_MAX_UPLOAD_BYTES` in `holdings.py`, 5 MiB):
  `Content-Length` fast-reject before reading any body, then a chunked
  read (64 KiB) that aborts once the running total crosses the cap —
  never materializes an oversized body fully into memory first.
- **Known accepted gap**: no retention/cleanup for successful `preview`
  JSONB rows (Ring 0, small row count — revisit before Ring 1).

### Holdings encryption at rest (issue #31)

Ring 0 audit deferred item, implemented 2026-08-09 as a Ring 1 prep gate.
Field-level, application-layer encryption via SQLAlchemy `TypeDecorator`
(`app/core/encryption.py`) — not Postgres transparent disk encryption, since
the threat is DB-dump/backup theft, which disk encryption alone doesn't stop.

- **Key scope decision: one system-wide key, not per-user.** The threat this
  protects against (disk/DB-dump theft) is independent of who's logged in.
  Per-user key isolation is a materially bigger design (key wrapping,
  rotation, loss-of-access recovery) with no concrete driver yet — building
  it now would be speculative. Revisit only when there's an actual
  multi-tenant isolation requirement, not just because a user system exists.
  Full rationale in Obsidian `Portfonia Concept & Design.md`, §5 Ring 0→1
  addendum (2026-08-09).
- **Algorithm: Fernet** (`cryptography` lib), not raw AES-GCM — Fernet
  manages IV/nonce generation internally, removing a misuse class hand-rolled
  AEAD invites for field-sized values. `HOLDINGS_ENCRYPTION_KEY` (Settings,
  `SecretStr`) is the active key; `HOLDINGS_ENCRYPTION_KEY_PREV` (optional) is
  read via `MultiFernet` during a rotation window — encrypt always uses the
  first (current) key, decrypt tries every configured key. No key-version
  column; rotation today only protects reads against a key swap, there is no
  bulk re-encryption pass (would be needed before dropping `_PREV`).
- **Scope — `Holding` columns encrypted**: `name`, `ticker`, `fund_code`,
  `shares`, `avg_cost`, `current_value`, `market_price`, `broker`, `account`,
  `portfolio`, `notes`. **Not encrypted**: `asset_type`/`asset_class`/
  `sector`/`market`/`currency`/`pricing_mode`/`position` — classification
  buckets, not individually identifying, and left queryable because
  `price_fetcher.py`/`price_capture.py`/`price_anomaly_detector.py`/
  `fund_nav_fetcher.py`/`window_data.py` all filter
  `Holding.ticker`/`Holding.fund_code` with SQL-level `.is_not(None)` —
  NULL-ness must stay visible at the SQL level (this still works fine under
  encryption; only value-level SQL equality/ordering breaks).
- **`ORDER BY` on encrypted columns breaks at the SQL level** (ciphertext
  sorts as ciphertext, not as the real value). `holdings.py`'s
  `list_holdings`/`export_holdings` used to `.order_by(Holding.asset_type
  .nulls_last(), Holding.name)` in the query; now fetch un-ordered and sort
  the already-decrypted Python objects via `_sorted_holdings()`. Same
  constraint applies to any future `.filter_by(ticker=...)`-style equality
  query on an encrypted column — fetch-then-filter in Python instead (two
  existing tests hit exactly this and were fixed:
  `test_price_fetcher.py::test_backfill_unknown_sector_becomes_other`,
  `test_fund_nav_fetcher.py::test_official_nav_parsed_and_anchored_to_cst`).
- **Migration** (`379fdb627ee8_encrypt_holdings_at_rest.py`): already-`Text`
  columns are encrypted in place (no type change); `Numeric` columns
  (`shares`/`avg_cost`/`current_value`/`market_price`) go through an
  add-populate-drop-rename sequence since a Fernet token isn't a valid
  numeric literal. Both directions verified against real local dev data
  (22 holdings, including a Chinese fund name) — round-trips exactly,
  including `upgrade → downgrade → upgrade`.
- **Side effect discovered 2026-08-09 (issue #25/#113): a DB-level `>= 0`
  CHECK on `shares`/`avg_cost`/`current_value` is no longer possible.** These
  columns are now Fernet ciphertext (`impl = Text`), so the database never
  sees the plaintext number — only `ParsedRow`'s `Field(ge=0)` in
  `app/schemas/holdings.py` guards this now, and only for writes going
  through `POST /holdings/confirm` (the only user-writable entry point for
  these fields). This tradeoff wasn't called out when the encryption
  decision was made — see issue #113 for the full writeup. Treat this as a
  standing lesson: framing a change as "must do" doesn't mean it has no
  costs elsewhere — say what breaks, not just what it fixes.
- **Not yet covered**: `Report.report_inputs` and rendered report bodies
  still carry holdings-derived plaintext (ticker, shares, values quoted in
  prose/tables) — issue #31 is scoped to the `holdings` table only. Worth a
  follow-up issue before treating "holdings encrypted" as covering the whole
  data surface; the public FAQ copy (PR #98) already says plainly that data
  is not yet encrypted at rest — needs updating once this PR merges and
  deploys, and again if/when reports are covered too.
- **Prod key generated and deployed 2026-08-09**, alongside the "生产部署"
  run that shipped this feature — a dedicated key, never copied from
  `.env.local`'s dev value (per "single system-wide key" above, "system"
  means per-environment, not one key shared dev↔prod). Verified against
  real production holdings: `docker compose exec backend python -c "..."`
  reading `Holding` rows through the live app process decrypted correctly
  (26 rows). `.env` never travels through Git (see Secrets and
  Configuration below) — the key lives only in the server's `.env` and the
  local `.env.production` staging copy, neither committed.

### Holdings domain CHECK constraints (issue #25)

DB-level `CHECK` constraints on `holdings`, migration
`6cd7544f63cf_add_domain_check_constraints_to_holdings.py` — previously only
app-layer (`ParsedRow` Pydantic `Literal`s) guarded correctness, so any write
bypassing the API (a script, a manual `UPDATE`) had no guard at all.

- **Covered**: `pricing_mode` (`auto`/`manual`), `asset_type` (nullable, 6
  values), `currency`, `asset_class`. `asset_class` wasn't in the original
  issue text but is the same shape (Text column, closed set already defined
  in code) — folded in rather than deferred to a separate issue, since the
  user's stated preference is to bundle same-pattern low-risk work now
  rather than risk it being forgotten later.
- **`currency`**: not ISO-4217-exhaustive — `VALID_CURRENCIES` in
  `app/schemas/holdings.py` is a fixed list of currencies plausible for this
  product's holdings (the three natively-supported markets' currencies —
  USD/CNY/HKD/CNH — plus other majors an international account might carry:
  GBP/EUR/JPY/SGD/AUD/CAD/CHF/KRW/TWD/MOP/NZD). **CNH (offshore yuan) was
  missing from the first pass** — caught in PR #114 review round 1: it's
  distinct from CNY and already first-class in `portfolio_calculator.py`'s
  `_CURRENCY_TO_FX_PAIR`/`fx_fetcher.py`'s `USDCNH` pair, so omitting it was
  a real regression for a supported FX pair, not just an incomplete list.
  Adding a currency is a code change + migration, same pattern as
  `VALID_ASSET_CLASSES` (`app/services/asset_class_config.py`) — not a
  config edit. `ParsedRow.currency` also gets a matching `field_validator`
  so an unrecognized currency 422s at the API boundary instead of hitting
  the DB as a raw `IntegrityError`; `asset_class` gets the same treatment
  (round 1 finding — it had the DB CHECK but no app-layer validator, so a
  forged value on `POST /holdings/confirm` could 500 instead of 422).
  `VALID_PRICING_MODES`/`VALID_ASSET_TYPES` are derived from `ParsedRow`'s
  existing `Literal` types via `typing.get_args()` rather than hand-copied,
  so model/migration/schema can't drift apart (also a round-1 finding).
- **Currency validation degrades per-row, doesn't fail the whole upload**
  (round 1 finding): `_postprocess` in `holding_parser.py` normalizes
  currency case/whitespace before the `VALID_CURRENCIES` check (matching
  the existing `asset_type`/`market` normalization), and a row that still
  fails `ParsedRow` validation is dropped into `issue_rows` via an
  `on_invalid_row` callback rather than raising and killing every other
  valid row in the same file. The unrecognized-currency check itself runs
  **last**, after ticker-suffix correction (round 2 finding) — otherwise a
  wrong-but-fixable value (e.g. `"RMB"` on a `.HK` ticker) left a stale
  "Unrecognized currency" note on a row whose final currency was valid.
- **`shares`/`avg_cost`/`current_value` `>= 0` is explicitly OUT of scope
  here** — see the "Side effect discovered 2026-08-09" bullet in the
  encryption section above and issue #113. Handled instead via
  `Field(ge=0)` on `ParsedRow` (app-layer only, DB can't enforce this
  anymore).
- **Naming-convention gotcha** (cost real debugging time — worth remembering
  for the next CHECK constraint added anywhere in this codebase): `Base`'s
  `naming_convention` (`app/models/base.py`, `"ck":
  "ck_%(table_name)s_%(constraint_name)s"`) re-renders whatever name is
  passed to `op.create_check_constraint`/`op.drop_constraint` in a migration,
  or to `CheckConstraint(name=...)` in an ORM model. Passing an
  already-fully-rendered name (e.g. `"ck_holdings_pricing_mode"`) doubles the
  prefix (`ck_holdings_ck_holdings_pricing_mode`) — pass the bare column
  token (`"pricing_mode"`) instead and let the convention render it. Verified
  against a real Postgres run both ways (doubled name confirmed, then fixed)
  before this migration/model landed.
- **Migration freezes a literal snapshot, does not live-import `VALID_*`**
  (round 2 finding): an earlier version imported `VALID_CURRENCIES` etc.
  directly into the migration, which meant `alembic upgrade head` on a
  fresh database run after those constants changed elsewhere (without a
  matching new migration) would silently produce a *different* CHECK
  constraint than an already-migrated database has — migrations must be
  immutable historical snapshots, not re-derived from current code state.
  Widening any of these sets is a **new migration**, never an edit to this
  one or to the source constant alone.
- `_in_list_sql` (in both the migration and `app/models/holding.py`) sorts
  its value list internally so every `CheckConstraint`'s SQL text is
  deterministic regardless of the source constant's declaration order —
  two of the four constraints were previously unsorted in the ORM model
  while the migration sorted all four (round 2 nit), which could have
  tripped `alembic revision --autogenerate` as spurious drift.
- **Audited existing dev rows before writing the migration** (2026-08-09,
  `portfonia_dev`, 22 rows) — all values already fit the new constraints, no
  data blocked the migration. **Production audited and deployed 2026-08-10**:
  same `GROUP BY`-per-column query run against prod first (26 rows, all
  conformant — no values outside the new constraints), then deployed via
  the standard `systemd-run docker compose up -d --build` flow. Verified
  beyond `/health`: `docker logs portfonia-migrate` confirmed the migration
  reached `6cd7544f63cf`, `\d holdings` on the live DB shows all four CHECK
  constraints (including CNH in the currency list), and a real
  `UPDATE ... SET pricing_mode = 'bogus'` against production (inside
  `BEGIN`/`ROLLBACK`, no data touched) raised
  `violates check constraint "ck_holdings_pricing_mode"` — the constraint
  is live, not just present in the migration file.
- Verified the constraint actually blocks a bad write at the SQL level (not
  just "tests are green"): the same direct `UPDATE` check above was also
  run against a real local-dev row first, before the production check.
- **Provenance**: two rounds of independent code review (blacktomb42) on
  PR #114 — round 1 found 1 real bug (the CNH gap above) + 3
  suggestions/nits, round 2 (after fixes) found 0 bugs + 2 suggestions/2
  nits, all verified against actual code and fixed. Both PENDING reviews
  submitted with resolution replies posted inline on each finding, plus a
  top-level PR comment summarizing both rounds — commit messages alone
  don't surface resolution status on the PR page (lesson now codified
  globally in `~/.claude/CLAUDE.md`'s Grok review workflow, step 6.5).

### Cash/wmf holdings silently excluded from reports (issue #120/PR #121)

Surfaced by a real "missing price data" ops notice on a production report
(2026-08-07). Cash/wmf are deliberately excluded from the capture layer
(`price_anomaly_detector.py`: "cash/wmf have no daily price") — the
notice's `stale_tickers` list was not a capture-layer miss, it was
`compute_portfolio()`'s manual-pricing branch excluding a row for missing
`current_value`.

- **Root cause**: `holding_parser.py`'s system prompt says a cash/wmf row
  should have no ticker and the amount in `current_value`. The structured
  extraction model (`STRUCTURED_LLM_MODEL`) didn't reliably follow this —
  verified against the actual affected production rows (decrypted via the
  `backend` app process, since `ticker`/`shares`/`current_value` are
  Fernet ciphertext at rest, issue #31) and against the original upload
  text: two of three rows had a ticker `"CASH"` fabricated out of thin air
  (not present in the source text at all), the third echoed a stray
  literal `"CASH"` token from the source into the ticker field. All three
  had the amount sitting in `shares` with `current_value=None`.
  `compute_portfolio()`'s manual branch only ever reads `current_value`,
  never `shares` — silent exclusion from every report and from the
  portfolio total, not just a report-rendering issue.
- **Fix, two independent layers** (`_postprocess` can't be the only guard —
  `POST /holdings/confirm` takes `list[ParsedRow]` straight from the
  client and bypasses it):
  1. `_postprocess` (`holding_parser.py`) deterministically coerces any
     `asset_type in ("cash", "wmf")` row: strips a spurious ticker/
     fund_code, moves the amount from `shares` to `current_value` when
     `current_value` is still null, forces `pricing_mode="manual"`, and —
     once `current_value` is settled as the source of truth — clears any
     residual `shares`/`avg_cost` unconditionally (round 2 finding below:
     leaving them populated isn't inert, see below).
  2. `ParsedRow` (`schemas/holdings.py`) gets a `model_validator`
     (`_cash_wmf_boundary`) rejecting the same three failure shapes
     (non-null ticker/fund_code, `pricing_mode != "manual"`,
     `current_value is None`) for cash/wmf rows — a clean 422 for any
     caller that bypasses `_postprocess`, same boundary-validation
     pattern as issue #25's currency/asset_class checks.
- **Two rounds of Grok review** (blacktomb42): round 1 (Request changes,
  1 bug + 2 suggestion/nit) found the `ParsedRow` validator's first draft
  only checked ticker/fund_code — a confirm payload with the amount only
  in `shares`, or `pricing_mode="auto"`, still passed and still
  silent-dropped; verified by directly constructing the failing
  `ParsedRow` before fixing. Round 2 (Approve, 0 bug + 1 suggestion/nit,
  required since round 1 found a bug) found round 1's "leave a
  dual-populated row's `shares`/`avg_cost` in place, it's inert" claim was
  wrong for the upload-preview cost-basis summary: `_row_cost_basis()`
  (used by `_summarize()`'s broker subtotals, not by `compute_portfolio`)
  prefers `shares*avg_cost` over `current_value` whenever both are
  non-null, so a residual pair could surface a wrong preview number even
  though report valuation itself was already correct — fixed by
  unconditionally clearing `shares`/`avg_cost` once `current_value` is
  settled, verified red against the round-1 code first.
- **Known gap, not covered by this PR**: three existing production rows
  for one user still have the pre-fix shape (amount in `shares`,
  `current_value` null) — not data loss (`shares` holds the right number),
  tracked in issue #123 for a backfill or waiting on the user to
  re-confirm holdings.

### Capture layer + incremental reporting (ADR-002)

Full spec in Obsidian: `Hermes/Portfonia/Docs/Incremental Report & Capture Layer Design.md`.

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
- Which holdings map to which class: `_TICKER_ASSET_CLASS` in `app/services/holding_parser.py`.

`ticker_themes` table maps ticker/fund_code → theme for multi-holding
aggregation (e.g. QQQM + 019547 both `nasdaq_100`). Seeded themes:
`nasdaq_100`, `sp500`, `gold`, `japan_equity`, `tbill`.

Fund holdings (fund_code only, no ticker) participate in anomaly detection
via Tiantian Fund historical NAV: `fund_nav_fetcher.fetch_nav_history()` (lsjz API)
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
overrides are a Ring 1 decision, documented in `Portfonia Concept & Design.md`, not
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
  (the legacy market-data/news/analysis marker tags stripped by
  `report_generator._STRAY_TAGS`, sourced from `i18n_glossary.yml`'s
  `legacy_removed_markers_zh`, or `[S#]`). The system prompt forbids the model from
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
- **`frontend/public/` must stay non-empty**: git doesn't track empty
  directories; `frontend/Dockerfile`'s runner stage does
  `COPY --from=builder /app/public ./public`, which fails hard if the
  directory doesn't exist in the build context at all. Keep at least one
  tracked file there (a `.gitkeep` is fine) even after removing every real
  asset. (issue #100/#101 — this landed with PR #93 and sat undetected on
  `main` through two more PRs, because production hadn't redeployed since;
  `npm run dev`/`next build` don't care about a missing `public/`, only the
  Docker multi-stage build does — see the Quality Gates gap noted below.)

## Architecture

| Layer | Choice |
|-------|--------|
| Frontend | Next.js + shadcn/ui |
| Backend | Python FastAPI |
| Database | PostgreSQL, self-hosted in Docker on the production VPS (not Supabase-managed — decided 2026-08-05 to cut hosting complexity). Supabase is used for **Auth only**. |
| Task queue | Celery + Redis |
| LLM | Pluggable (Claude / DeepSeek / etc.) — keep provider-swappable |
| Local dev | Homebrew PostgreSQL 16 + Redis (native); Colima for Hermes gateway only |
| Production | Self-hosted on a free-tier cloud VM, Ubuntu 24.04 LTS. Provider, region, instance identifier, and IP are deliberately **not tracked in this repo** — see Obsidian doc below. |

### Three-layer deployment flow (MANDATORY)

**Full workflow + production server specs (provider, region, instance name,
IP, SSH user, remote paths) live ONLY in Obsidian `Hermes/Portfonia/Portfonia Environment Config.md`
— never in this repo.** This file (`CLAUDE.md`) is git-tracked, so it must
never carry any traceable identifier for the production host: no IP, no
cloud provider/region, no instance name, no SSH username, no remote
filesystem path. Look those up in the Obsidian doc before running any of the
commands below — they're written here with the specifics deliberately
omitted.

The one hard rule that governs every action here: code authority is
**local → Git only**. Never edit code on the production server, never `git
commit` there, never use it as a sync hub between machines — its only
legitimate local state is `.env` (uploaded via `scp`).

**The free-tier spec ceiling drifts — re-verify before assuming a number.**
The provider has silently cut the free-tier allocation more than once
without notice. Don't hardcode a spec number from memory or from an old note
here — check the provider's console/CLI before planning capacity.

**SSH stays open, guarded by fail2ban only** (`maxretry=10`, `findtime=10m`,
`bantime=10m` — relaxed from defaults after a prior fail2ban lockout on a
different service cost hours to recover from serial console). No source-IP
restriction: the dev machine has no fixed IP, and an agent session's own
egress IP isn't stable across runs either. If a future session gets banned
mid-task, the ban self-clears in 10 minutes — don't burn time trying to
route around it via the provider's serial console unless the task can't
wait.

**The dev-machine → production path is unreliable — not the production
server itself.** The production server has no known network problems on its
own connection to the internet or to OpenRouter. What's flaky is
specifically the link from the local dev machine to it, which routes
through the user's VPN/TUN proxy (confirmed 2026-08-06: SSH from this
machine repeatedly dropped mid-command while the server's own load/network
were fine). This means: **the connection can drop mid-command with no
warning** for anything originating from the dev machine (Claude Code's own
SSH, and likely the user's browser too, if it routes through the same
proxy) — two separate `docker compose up --build` launches died silently
mid-build this way, one via `nohup ... & disown` on the remote side, one via
keeping the SSH session itself alive locally with `run_in_background` —
neither survives an actual network drop, because both still depend on the
TCP/SSH connection staying up long enough to hand off. Don't extrapolate
this to "the production site is unreliable for real users" — a real user
connecting independently over the open internet doesn't go through this
proxy path. **For any remote command expected to run longer than a few
seconds, use `systemd-run` on the server** so the command runs as a
transient unit fully independent of the SSH session — get the exact
host/user/path from the Obsidian doc, then:

```bash
ssh <host-from-obsidian-doc> "sudo systemd-run --unit=portfonia-deploy --working-directory=<path-from-obsidian-doc> -- docker compose up -d --build"
# reconnect any time after, even following a dropped connection, to check on it:
ssh <host-from-obsidian-doc> "systemctl status portfonia-deploy; sudo journalctl -u portfonia-deploy --no-pager"
```

Do not trust a `nohup`/`disown`/backgrounded-SSH exit code as proof a long
remote command finished — verify by checking the actual resulting state
(containers running, files present), not just the shell's reported exit
status.

**An explicit, unambiguous request to deploy the currently-merged `main` to
production — in whatever language or phrasing the requester uses — means
execute this procedure** (established 2026-08-06, after the first
successful full-stack deploy). The human workflow ends at PR merge to
`main` (branch → implement → test → PR → review → fix → merge, all local);
production deployment is the one additional step that ships a merged `main`
to the production server:

1. Sanity-check local `main` is clean and matches `origin/main` (don't
   deploy stale/uncommitted state).
2. SSH in (host/user/path from the Obsidian doc), `git pull`.
3. `sudo systemd-run --unit=portfonia-deploy --working-directory=<path> -- docker compose up -d --build`
   — always systemd-run, never a plain foreground/backgrounded SSH command,
   regardless of how small the change looks (a `--build` with no
   dependency changes is fast due to layer caching, but the connection can
   still drop mid-command).
4. Poll for completion, tolerating transient SSH check failures (retry the
   check, don't treat a dropped check-connection as deploy failure) but
   treating an actual `exited (1/2/137/139)` container or a `failed`
   systemd unit as real failure.
5. `curl https://api.portfonia.com/health` — confirm `{"status":"ok",...}`.
6. Report success (what changed) or failure (which step, what the logs
   showed) — don't declare done without step 5 passing.

**Before step 3, check whether the commits being deployed add a new
required `Settings` field** (`app/core/config.py` — no default, not
`| None`). If so, that value must already be in the server's `.env` (or be
added there — a fresh key, never copied from `.env.local`'s dev value)
*before* `docker compose up -d --build` runs, or `migrate`/`backend`/
`celery-worker`/`celery-beat` will all fail Pydantic validation at
container start. `docker-compose.yml`'s `migrate` service (one-shot
`alembic upgrade head`, gated by `depends_on: postgres:
service_healthy`) runs before `backend`/`celery-worker`/`celery-beat`
via `condition: service_completed_successfully` — so a missing required
var fails cleanly (migrate exits non-zero, dependents never start) rather
than partially starting, but it's still a failed deploy. Confirmed this
gate actually encrypts real production rows correctly (issue #31 deploy,
2026-08-09): `docker compose exec backend python -c "..."` reading
`Holding` rows through the live app process, not just `/health`, is the
right depth of check when a migration transforms existing data — a green
`/health` alone doesn't prove the migration ran or that decryption works
against real rows.

**`systemd-run --unit=portfonia-deploy` fails with "Unit ... was already
loaded or has a fragment file" if a previous attempt's unit is still
registered in a `failed` state** (systemd doesn't auto-clean failed
transient units — only successful ones vanish). Hit this 2026-08-09 from a
stale unit left over from the unrelated 2026-08-07 `frontend/public/`
build failure (issue #100/#101, long since fixed). Fix: `sudo systemctl
reset-failed portfonia-deploy` before retrying `systemd-run` with the same
unit name — don't rename the unit to dodge this, the reset is one command
and keeps the naming convention stable for the next session's `systemctl
status portfonia-deploy` check.

**A second, unrelated instance exists in the same cloud tenancy — never
touch it** (stop/resize/reconfigure/reuse) when working on Portfonia infra.
It belongs to a different project and sits in its own isolated network. See
the Obsidian doc to identify it if you need to confirm you're not touching
it.

### Env-only sync to production (no code change involved)

**An explicit request to push `.env` changes (secret rotation, config value
change) to the server without an accompanying code change is a separate,
smaller procedure from the code-deploy flow above** — established
2026-08-06, first used to roll out a rotated Resend key:

1. `scp` the local `.env.production` to the server's `.env` (path from the
   Obsidian doc) — `git pull` is irrelevant here, `.env` never travels
   through Git.
2. **`docker compose restart <service>` does NOT reload `env_file` values**
   — Compose only re-reads `env_file` when a container is *recreated*, not
   on a plain restart of an existing one. Recreate explicitly:
   `docker compose up -d --force-recreate <services>` — target only the
   services that actually declare `env_file: .env` in `docker-compose.yml`
   (currently `backend`, `celery-worker`, `celery-beat`; `frontend`/`caddy`
   don't and shouldn't be touched for an env-only change — minimal blast
   radius).
3. If the code-deploy procedure above (`docker compose up -d --build`) is
   running concurrently on the server, wait for it to finish before doing
   this — both operate on the same `docker compose` project and can race
   (`--force-recreate` on the same containers a build is replacing).
4. Verify: `curl https://api.portfonia.com/health`, then a real functional
   check of whatever changed (e.g. for an email-provider key rotation, exec
   into the `backend` container and send a real test message through the
   actual send path — don't just trust a 0 exit code from a function that
   swallows its own exceptions and logs to a stream `docker compose exec`
   won't show you; print the provider's raw HTTP response instead).

## Secrets and Configuration

- `.env` files are **never** committed. Enforce via `.gitignore` from day one.
- API keys (Claude, Resend, market-data providers) are loaded from `.env` only.
  Never hardcode, never log, never echo to stdout in error paths.
- For test code: never read or write the developer's real `~/.config/...`
  directories. Honor a project-scoped env var (e.g. `PORTFONIA_HOME`) and
  default tests to a temp dir. Direct use of `os.path.expanduser("~")` in
  code that tests will exercise is a bug.
- **Never commit a traceable production infrastructure identifier to this
  repo**: no real IP address, no cloud provider/region, no instance name/ID,
  no SSH username, no remote filesystem path — regardless of whether the repo
  is currently public or private (visibility can change, forks/clones
  persist regardless). This applies to `CLAUDE.md` and any other tracked
  file, not just code. The actual specs live only in the private Obsidian
  ops doc referenced from the deployment section below. (Incident:
  2026-08-06 — the production server's real IP, SSH user, remote path, cloud
  provider, and region sat in `CLAUDE.md` across 3 commits on this public
  repo for ~30 hours before being caught; history was rewritten and
  force-pushed to remove it, but that can't guarantee removal from caches,
  forks, or clones made in that window — treat anything like this as burned,
  not just hidden, once it's been pushed.)

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
- **`data_collection=deny` is applied to every LLM call by default** (not just
  holdings-bearing ones) as defense in depth: even if holdings leak into Pass 1
  in the future, the call still cannot route to training providers.
  **Exception (issue #78, 2026-08-06):** Pass 1 search-query generation and
  translation render — both on `LOW_COST_LLM_MODEL` — pass
  `enforce_data_collection=False` because they're routed via OpenRouter BYOK
  straight to DeepSeek's own first-party backend (`order=["DeepSeek"]`,
  `_BYOK_PROVIDER_ORDER` in `report_generator.py`), the exact provider `deny`
  exists to exclude. Translation carries holdings-derived report text
  (`with_holdings=True`); this was an explicit, scoped compliance tradeoff the
  product owner accepted for these two call sites only — Pass 2, regenerate,
  and holdings parsing (structured extraction) all keep `deny` enforced
  unchanged. Both call sites also pass `allow_fallbacks=False` (a hard pin,
  not a preference) alongside the `order` pin — since `deny` is off, an open
  fallback on DeepSeek unavailability could otherwise silently reroute the
  payload to an arbitrary marketplace provider that `deny` would normally have
  excluded; the call fails outright instead (PR #79 review finding). Do not
  extend the exception to any other call site without the same explicit
  sign-off, and never drop the `allow_fallbacks=False` pairing if you do.
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

**Gap this doesn't cover**: none of the above actually builds the Docker
images. A change that only breaks `docker build` (e.g. deleting the last
file in `frontend/public/` — see the regression note above) passes every
gate here and still fails at deploy time, silently, until someone actually
redeploys. When a change touches `frontend/public/`, either `Dockerfile`,
or `docker-compose.yml`, run a real `docker build`/`docker compose build`
before pushing — `npm run dev`/`next build` do not exercise the same path
and will not catch this class of bug.

## CI-First Protocol (MANDATORY)

> **Ring 0 reality:** there is no automated CI yet — the local quality gate
> (see above), run before every push, stands in for it. There IS a branch +
> PR for every change regardless of Ring (see Branching below); "CI green"
> currently means "local gate green" on the PR's branch.

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

> **2026-08-06 correction:** every change — code, config, or docs, at every
> Ring, no solo-work exception — starts on a branch and goes through a PR.
> The prior "Ring 0 commits directly to `main`" carve-out is retracted: it was
> read (incorrectly) as also licensing autonomous PR merges, and PR #79
> (issue #78) was merged without the product owner's sign-off as a result —
> reverted same day. **Merging any PR into `main` requires the product
> owner's explicit, real-time approval in the current conversation.** A green
> quality gate, a passed review (including a reviewer-identity self-review),
> or an issue/task description that says "implement and merge" are NOT
> themselves that approval — they make a PR ready to ask about, not ready to
> merge. Finishing a PR ends with "ready for your review" or "ready to
> merge?", not with `gh pr merge`.

```
main (production) ← dev (integration, Ring 1+ target — not yet in use) ← feat/* | fix/* | docs/*
                                                                           ↑
                                                                           hotfix/* (only emergencies, from main)
```

- Never commit directly to `main`. `dev` doesn't exist yet, so `feat/*` /
  `fix/*` / `docs/*` branches currently start from `main`; switch to
  branching from `dev` once it exists.
- `dev → main` promotion PRs must use `feat:` or `fix:` (a `chore:` title
  will not trigger a release).
- Delete branches after merge.
- **Stacked branches (branch B built on not-yet-merged branch A) + squash-merge
  is a known trap** (hit 2026-08-07, PR #93/#95/#96): squash-merging A with
  `--delete-branch` deletes A's branch, and GitHub **auto-closes any open PR
  whose base is that branch** — `gh pr reopen` / `gh pr edit --base` both fail
  once the base ref is gone (no recovery). If A merges before B is done, get
  B's commits onto `main` via `git merge main` (not `git rebase main` —
  replaying B's pre-squash commits against a squash-merged `main` produces
  spurious `add/add` conflicts, and `git rebase --skip` is a history-rewrite
  the auto-mode permission classifier blocks) and open a **fresh PR against
  `main`**, noting in its body which closed PR it supersedes. Also watch for
  a specific `git merge` footgun this surfaces: if B's branch added-then-
  removed something (e.g. moved a component out of a shared layout) before
  merging in A, the 3-way merge can silently **reinstate the removed code**,
  because B's net diff against the merge-base shows no change on those
  lines while A's does — re-check anything B deliberately deleted after
  merging.

## Issue Tracking (MANDATORY)

Every new feature/improvement request and every bug — regardless of whether
it's fixed immediately — gets a GitHub issue first, before the fix/feature
work starts. Issues are the project's request/bug ledger; the CLAUDE.md debt
table is for cross-session technical-debt reminders only, not a substitute.

- **Blocking / fix-now**: open issue → fix/implement → comment with commit
  hash + approach + verification → close.
- **Deferred**: open issue → leave in backlog → comment + close when later
  addressed.

**Two separate GitHub identities, don't mix them up** (actual accounts live
in `.env.local`, never committed — this file intentionally does not name
them): `GITHUB_TOKEN` is the primary write identity — repo owner, used for
commits/pushes, issue/PR creation, and merges. `GITHUB_REVIEWER_TOKEN`
(added 2026-08-06) is read + PR-review-only, and belongs to a **separate LLM
reviewer** in this project's multi-agent workflow — **this agent (whichever
LLM is doing the dev work) never uses `GITHUB_REVIEWER_TOKEN` itself, for
anything.** Any review or comment authored under that identity is that other
reviewer's independent output: read it, act on its findings, but its
approval is not a substitute for the product owner's own merge
authorization, and does not come from self-review. (Incident: 2026-08-06,
issue #78/PR #79 — this agent used `GITHUB_REVIEWER_TOKEN` to review its own
PR, then treated that as grounds to merge without the product owner's
sign-off. Reverted; see PR #79 for history.)

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
live in Obsidian `Hermes/Portfonia/Portfonia Concept & Design.md` §1 + appendix — not
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
