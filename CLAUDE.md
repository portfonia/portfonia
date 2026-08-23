# Portfonia — Agent Guidelines

AI-facing guidance for agent tooling working in this repository.
Last updated: 2026-08-20

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
| LLM model | OpenRouter, split by call shape (issue #78, 2026-08-06). **Structured/JSON** (holdings parsing, `holding_parser.py`, the only call site requiring schema-compliant output) = `STRUCTURED_LLM_MODEL` (`openai/gpt-5.6-luna` — moved off `google/gemma-4-31b-it` in issue #84, 2026-08-06: the gemma pin to OpenInference's bf16 endpoint was itself the latency bottleneck, 371s worst case on a 30-row holdings file; `gpt-5.6-luna` measured 10.9-13.8s on the same file with 30/30 rows correct on manual audit — one manual run, not yet a systematic eval), `reasoning_effort=none` (`_STRUCTURED_REASONING_EFFORT` in `holding_parser.py` — this model defaults reasoning to "medium", wasted cost/latency for mechanical extraction), open/unpinned provider selection for both of 2 identical attempts (`app/core/llm.py:structured_provider` — no precision-pin concern for this model, unlike gemma's third-party quantized resellers); `data_collection=deny` applies throughout. **Unstructured/free-text** (Pass 1 search-query gen, `report_prompts.py`/`report_generator.py` + translation render, `report_translation.py` — split from a single `report_generator.py` in issue #37) = `LOW_COST_LLM_MODEL` (`~deepseek/deepseek-v4-flash-latest` — leading `~` is OpenRouter's "-latest" alias convention), routed via OpenRouter BYOK straight to DeepSeek's own backend (`order=["DeepSeek"]`, module constant `_BYOK_PROVIDER_ORDER` in `report_llm.py`) with `enforce_data_collection=False` — a scoped compliance exception for these two calls only — **and `allow_fallbacks=False` (hard pin, no marketplace fallback)**: since `deny` is off for these calls, an open fallback on DeepSeek unavailability could silently reroute the (holdings-bearing, for translation) payload to a training-permitting provider `deny` would normally have excluded; the call must fail rather than degrade that guarantee (PR #79 review finding). Reasoning/thinking tokens are explicitly disabled (`disable_reasoning=True`) since this alias defaults reasoning on unlike the non-aliased model. **PRIMARY (Pass 2 analysis + regenerate) = `deepseek/deepseek-v4-pro`**, unchanged — provider=DigitalOcean,Venice, `data_collection=deny`, no BYOK. Sonnet/Anthropic models are NOT used here — too expensive (~$0.2/call); if `PRIMARY_LLM_MODEL` ever shows an `anthropic/*` value it is config drift, revert it. |
| Infrastructure | Homebrew PostgreSQL@16 + Redis (native, not Docker); `make infra-up` not needed |
| **App runtime retired locally (2026-08-10)** | No local uvicorn/celery worker/celery beat/Next.js dev server anymore — running the app for manual verification happens only via production deploy (see Three-layer deployment flow below). Homebrew Postgres/Redis stay running locally, but only as backing services for `pytest` (real-Postgres integration tests per the Tests section) — never as targets for a locally-running app process. Do **not** start `uvicorn`/`celery worker`/`celery beat`/`next dev` on this machine; if a task needs to be seen working, that means deploying to production, not spinning up a local server. The old "kill and restart uvicorn/celery after any model/migration/router change" drill no longer applies — there is no long-lived local process to go stale. |
| Output language | reason in EN, render in `OUTPUT_LANG` (Ring 0 default `zh`) via a translation pass with a fixed-term glossary — locale-keyed, single source of truth in `backend/config/i18n_glossary.yml` (`report_glossary`/`forbidden_renderings`; only `zh-Hans` populated today, schema reserves `zh-Hant`/`fr`/`es` for later); `en` = no-op |
| Report statuses | `success` · `skipped` (quiet day, still emails heartbeat — EXCEPT a short manual quiet window: `session_node="manual"` + <2h span + 0 news + 0 anomalies suppresses the heartbeat as a same-day re-run artifact) · `needs_review` (compliance scan hit, NOT emailed) · `failed` · `in_progress` |
| Report title / email subject | `Portfonia <Financial Analysis Report> — YYYY-MM-DD HH:MM ET` (title timestamp from `period_end`; the zh-Hans render substitutes the `report_glossary` term for "Portfonia Financial Analysis Report" from `i18n_glossary.yml`); no "Intelligence" wording, or its zh-Hans equivalent (`forbidden_renderings` in the same file), anywhere. |
| Holdings model | `market` + `broker` are user-declared fields; `position` preserves upload order. **§1 groups by `broker` (rendered as "Custodian" — zh-Hans term in `i18n_glossary.yml`'s `report_glossary`)** in upload order with per-institution subtotals; cash sits inside its institution, broker-less rows fall into "Other". `position` is populated automatically on confirm. |
| Holdings upload | Async, not a single blocking request — see "Async holdings upload" section below (issue #77/#82/#85). |
| Re-render | `regenerate_report(mode=render\|analyze)` rebuilds from stored `report_inputs` without re-fetching; `POST /reports/{id}/regenerate`. render = token-free, analyze = Pass 2 only. |
| §1 / distribution / §4.1 classification dimension | **`asset_class`** (geography-first taxonomy — see table below), not `sector` or `asset_type`. `sector` (yfinance GICS) is retained ONLY for forward-event holding-relevance mapping (rate-sensitive/consumer sectors for FOMC/CPI events) — never reintroduce it into §1/distribution/§4.1. `by_asset_class` has no "Other" fallback (every `Holding` always has one, default `STOCK`). |
| Tests must mock external notify calls | `send_ops_alert`, `create_bug_report`, `send_report_email` are mocked via an **autouse** fixture in `app/tests/conftest.py` (`_no_external_notifications`) — never rely on individual tests remembering to patch them. A gap here previously sent 42 real "FX rates stale" emails to the admin inbox from three same-day pytest runs (test clock fixed to a historical date that always trips the staleness check against the real current date). |

### Frontend chrome (header/nav) convention — implemented (issue #146/#148)

**Every route shares ONE header/nav component (`frontend/src/components/site-header.tsx`),
rendered once from the root `app/layout.tsx`.** Do not add a second
per-route header implementation — that duplication (`HomeNav` vs a separate
`SiteHeader`) is exactly the bug this convention fixed; see Obsidian
`Hermes/Portfonia/Portfonia Concept & Design.md` §10 addendum (2026-08-14)
for the full before/after and decision rationale.

- **Current shape**: `SiteHeader` renders as a `<header>` landmark
  (floating rounded pill, `sticky top-4`, backdrop-blur, dark). `AppShell`
  (`frontend/src/app/_components/app-shell.tsx`, renamed from the old
  home-only `HomeShell`) and `LocaleProvider` both wrap the whole app from
  root layout, not just the home page. Universal on every route:
  brand/home link, Holdings entry link (`href="/holdings"`, label sourced
  from `messages.holdings.pageTitle`). Home-only (`pathname === "/"`): the
  four marketing anchor links, the locale switcher, and the brand link's
  target changes to `#top` (in-page jump) instead of `/`.
- **`lang` attribute is route-scoped, not just component-scoped**:
  `AppShell` only follows the selected locale on `/`; every other route
  (still English-only via `lib/messages.ts`, no `zh` map yet) stays
  `lang="en"` regardless of what's stored in `localStorage` — a stored
  `zh` from the home page must never mislabel `/holdings`' English content
  for screen readers / in-browser translate. Covered by
  `app-shell.test.tsx`.
- **The locale switcher itself is home-only for the same reason**:
  `/holdings`' `lib/messages.ts` has no `zh` map yet; showing the switcher
  everywhere before that's fixed would let a user "switch language" on a
  page whose text never changes. Relax that gate once `messages.ts` gains
  a `zh` map — separate, unscheduled work.
- **Tests**: `frontend` had no test framework before this — vitest +
  React Testing Library were added specifically for this change
  (`npm run test`). `site-header.test.tsx` / `app-shell.test.tsx` lock the
  route-conditional rendering above; extend them, don't remove the
  route-parametrized assertions, if this component changes again.
- When adding a new route: it inherits the header for free by living
  under the root layout — do not wrap it in its own header/layout unless
  it has a genuine reason to opt out of the shared chrome (and if so,
  treat that as worth a design-doc note, not a silent second
  implementation).

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
- **Extracted-text size cap** (`_MAX_TEXT_BYTES` in `holdings.py`, 100 KiB,
  issue #54/PR #158): `_MAX_UPLOAD_BYTES` only bounds the raw uploaded
  file — a high text-to-byte-ratio file (e.g. an `.xlsx`/`.xls` that
  unpacks into a much larger CSV) could still extract to far more text
  than any real holdings file, with nothing bounding what actually
  reached the LLM or got persisted to `UploadJob.raw_text`. Checked via
  `len(text.encode("utf-8"))` right after `_extract_text()`, before
  persist/enqueue — 422 on overage, matching this endpoint's existing
  convention. Byte-based (not `len(text)`) deliberately, since this
  product's mainland broker/fund exports are often CJK — a char-count
  check would accept a payload well over the real byte budget; locked by
  a CJK-content regression test, not just the ASCII off-by-one pair.
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

### Postgres backup to OCI Object Storage (issue #106/#76, PR #122)

Self-hosted Postgres (2026-08-05 decision) had no backup safety net at all
until this shipped. Daily `pg_dump -Fc` → `oci os object put`, Celery beat
03:00 ET (`backup-database-daily`), 30-day retention enforced by the
bucket's Object Lifecycle Policy — not application code, to avoid a second
place for that number to drift.

- **Auth: OCI instance principal, not an API key.** Production runs the
  task on the app VM itself, so `--auth instance_principal` is added
  whenever `APP_ENV == "production"` (`_oci_auth_args()` in
  `app/services/db_backup.py`) — no OCI key file ever touches the server.
  Local/manual runs (e.g. a restore drill) fall back to the CLI's default
  `~/.oci/config`.
- **`pg_dump` and the `oci` CLI are invoked as subprocesses, never imported
  as Python libraries.** `oci`/`oci-cli` pin `cryptography<50.0.0`, which
  conflicts with this app's `cryptography==50.0.0` pin (Fernet holdings
  encryption, issue #31) and would silently downgrade it on install.
  `backend/Dockerfile`'s runtime stage installs `postgresql-client-16`
  (via the official PGDG apt repo, matching `postgres:16-alpine` exactly)
  and `oci-cli` in a fully isolated venv (`/opt/oci-cli`) — never in
  `requirements.txt`, never in the app's own `/venv`. Verified in the built
  image: app venv keeps `cryptography==50.0.0`, `/opt/oci-cli` has its own
  independent `cryptography==46.0.7`.
- **Production fails loud, not open, on misconfiguration.**
  `backup_database()` raises `BackupError` if `BACKUP_OCI_NAMESPACE` is
  unset (or whitespace-only) AND `APP_ENV == "production"` — this is the
  only DB restore safety net, so a missing/typo'd env var must not produce
  a daily "success" that backed up nothing. Local dev (namespace unset by
  default) silently no-ops instead, so an accidentally-started local Beat
  never uploads dev dumps anywhere.
- `backup_database_task` (`app/tasks/backup_tasks.py`) mirrors
  `capture_tasks.py`'s retry/ops-alert pattern (`max_retries=2` →
  `send_ops_alert` + GitHub issue on exhaustion), with one addition:
  `SoftTimeLimitExceeded` is caught separately and alerts immediately
  without retrying — a soft timeout at 920s means the attempt already
  burned nearly its full budget, so retrying would just delay the alert by
  up to two more ~920s attempts on a task billed as the only safety net.
  `time_limit`/`soft_time_limit` (960s/920s) are set above the sum of
  `db_backup.py`'s own subprocess timeouts (600s dump + 300s upload = 900s)
  — a soft/hard limit below that sum would fire mid-subprocess via signal,
  which `subprocess.run`'s own `timeout=` cleanup never sees, risking an
  orphaned `pg_dump`/`oci` child process.
- **Verified with two real restore drills** (issue #106 explicitly requires
  this — a backup script that's never been restored from is not a safety
  net): one locally against dev data, one against real production data
  entirely within the production server's boundary (no user data left the
  host). Both: dump → upload → download → `pg_restore` into a scratch
  database → row counts match the source exactly across all 8 tables →
  app-level Fernet decryption of holdings (via `SessionLocal` + `Holding`
  model pointed at the scratch DB) matches the source byte-for-byte,
  including CJK content. Full runbook + both drill writeups: Obsidian
  `Hermes/Portfonia/Portfonia Environment Config.md`.
- **Real production bug found during the production drill, not by either
  code-review round**: the OCI IAM policy scoping instance-principal access
  to the bucket was created via `oci iam policy create --statements
  '[...where target.bucket.name=\'portfonia-db-backups\'...]'` — bash's
  outer single-quote wrapping terminated early at the inner single quotes,
  silently submitting an **unquoted** string literal. OCI's policy engine
  accepts this with no error but the condition then never matches any real
  request — every object-storage call returned `BucketNotFound` (404),
  which reads exactly like a missing bucket, not a permissions gap, since
  OCI deliberately returns 404 rather than 403 for unauthorized access.
  Fixed via `oci iam policy update` with the value quoted, **verified by
  reading back the stored statement text** (`oci iam policy list --query
  .statements`) rather than trusting the update command's own success —
  full diagnostic writeup in Obsidian (same doc). Lesson generalizes beyond
  this project: never pass a CLI arg requiring nested quotes as an inline
  bash single-quoted string; use `file://` instead.
- **Provenance**: two rounds of independent code review (blacktomb42) on
  PR #122 — round 1 found 2 real bugs (a production instance name
  committed in a docstring, violating this repo's own infra-identifier
  policy; the pre-fix silent-skip-in-production behavior) + 4
  suggestions/nits, round 2 (after fixes) found 0 bugs + 4 suggestions/2
  nits, all verified against actual code and fixed. Both PENDING reviews
  submitted, resolution replies posted inline on each finding, plus a
  top-level PR comment per round.

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

### Fund NAV realtime path: Sina Finance fallback (issue #20)

`fund_nav_fetcher.py`'s realtime path (`_fetch_nav` → `update_fund_navs`, keeps
`holding.market_price` current) reads Tiantian Fund's `fundgz.1234567.com.cn`
JSONP endpoint. As of 2026-08-10, direct `curl` from the OCI production host
confirmed this endpoint returns Eastmoney's app-layer block page (HTTP 200,
HTML "页面未找到") for every fund code — not a network-reachability gap, an
actual block, and the historical path (`fetch_nav_history`/lsjz, used by
`capture_fund_navs` for `price_snapshots`) is unaffected and confirmed
reachable from the same host. `_fetch_nav` now falls back to Sina Finance
(`hq.sinajs.cn/list=f_{code}`, GBK-encoded) when fundgz fails, via a new
`_sina_fund_nav` helper (two-attempt retry, increasing timeout) ported from
the sibling `portfolio-agent` project's `collector_v2.py::_sina_fund_nav`,
which hit and solved this same block on 2026-07-30 and cross-validated Sina's
numbers against Tencent's `qt.gtimg.cn/q=jj{code}` as a second source.

- **Scope**: only the realtime path. `fetch_nav_history`/`capture_fund_navs`
  (lsjz) needed no fallback — confirmed working, left unchanged.
- **Verification**: production `curl` (two fund codes, two Eastmoney block
  hits, one Sina success matching the expected `name,nav,nav,cum_nav,date,...`
  shape) before writing any code; TDD red→green in the worktree afterward.
  PR #134 review (blacktomb42, Approve, 0 bugs / 2 suggestions / 1 nit) added
  3 more red→green tests covering the fixes: fundgz's per-fund block-page log
  downgraded ERROR→WARNING (only the terminal both-sources-fail case logs
  ERROR now), Sina retry scoped to `httpx.HTTPError` only (a parsed-but-bad
  200 returns immediately, no wasted second round-trip), and the Sina quote
  regex anchored on `hq_str_f_{fund_code}=` instead of "any first quoted
  string". 10 tests total in `test_fund_nav_fetcher.py`, full backend suite
  green (499 passed).
- **Test-infra gotcha hit while adding the log-level tests**: see "Tests"
  section below (`caplog` + alembic `fileConfig`).
- **Separate finding, not fixed by this change, tracked as issue #135**:
  while investigating, 3 production holdings (fund_codes
  019547/110011/008142, `pricing_mode=auto`) were found with 0
  `price_snapshots` rows despite `lsjz` being reachable and
  `capture_fund_navs` working correctly when invoked manually in production
  (70 rows written on manual run) — the scheduled `capture-fund-navs-daily`
  beat task (`0 20 * * mon-fri`) had not populated them across at least 2
  scheduled windows. Root cause not established (celery-beat/worker had been
  recreated ~1h before diagnosis by an unrelated deploy, wiping the logs that
  would show why). As of this note (2026-08-13, ~19:35 CST) the 20:00 CST
  run that would be the next real test hadn't happened yet — check issue
  #135 for the outcome rather than assuming either way.

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
  News selection is NOT range-bounded by this watermark on the lower end —
  see "News dedup ledger" below (issue #30) for why.
- **Cadence:** `generate_incremental_report` fires Mon/Wed/Fri 17:00 ET
  (moved from 16:30 ET on 2026-06-19, widening the gap after the 16:05 ET
  FX capture and 16:00 ET close capture).
- **Multi-user fan-out (Ring 1 stage A1, issue #128, PR #151)**:
  `generate_incremental_report` now iterates `app.services.user_scope.
  active_user_ids` (`SELECT DISTINCT user_id FROM holdings` — no `User`
  table yet, see design doc) instead of the old fixed `DEV_USER_ID` single
  call. Each user's `generate_report` call is isolated in its own
  try/except: one user's failure is ops-alerted and logged, does NOT stop
  or retry the rest of the batch — but if EVERY user in a batch fails, the
  task still escalates to the normal 3x/5-minute Celery retry rather than
  reporting a false "completed". `generate_report(user_id=..., moves_cache=...,
  now=...)` — all three optional, `None`/omitted preserves the old
  single-user behavior for any other call site (manual trigger, tests).
- **Anomaly detection is split into a global pass + a per-user pass**
  (issue #128 A1): `window_data.compute_global_moves` computes each
  identifier's window price move exactly ONCE per batch (shared across
  every user who holds it, via a `moves_cache` keyed on the exact
  `(period_start, period_end)` window — `generate_incremental_report`
  stamps one shared `now` for the whole batch so this cache key actually
  collides across users); `window_data.select_user_anomalies` then does the
  per-user threshold judgment (two users can classify the same identifier
  under different `asset_class`, so the same move can clear one user's
  threshold and not another's) + theme merge, in-memory, no DB/LLM calls.
  `detect_window_anomalies(session, start, end, user_id, moves_cache=None)`
  is the thin single-user wrapper — `user_id` is now a required argument;
  the pre-A1 signature queried ALL holdings with no user filter at all,
  which was a real cross-user data leak once more than one user exists.
  Fires on EITHER trigger: `single_day` (one trading day beyond per-class
  per-day threshold — catches a violent session a net move would smooth
  away) or `cumulative` (baseline→latest net move beyond a scaled, capped
  threshold). Flagged holdings carry a session arc (prev close/open-gap/
  intraday range/close/after-hours) for §4.2. Report always says "this
  report period", never "today".
- **Tavily fan-out fairness (issue #128 A1 finding, resolved by A2's
  `search_cache`)**: the shared Tavily daily search budget used to be
  consumed sequentially in fixed `active_user_ids()` UUID order within a
  fan-out batch — users processed later in a given day could inherit
  `daily_remaining=0` and get no search-augmented context. Root-cause fix
  was reuse/dedup, not reallocating a fixed-size budget — see the L1
  shared-intel entry below.
- Portfolio valuation reads the **latest captured close** from
  `price_snapshots`, falling back to `holding.market_price` only for funds
  (no ticker). FX anomalies are not computed (FX stays daily in `fx_rates`).
- **L1 shared ticker-intel cache (Ring 1 stage A2, issue #128, PR #155)**:
  a new analysis stage (not a refactor — before A2, per-identifier "what
  happened to this security" narrative only existed inside each user's own
  Pass 2 call). `ticker_intel.get_l1_intel_batch` computes one LLM analysis
  per `(identifier, trade_date, prompt_version)` and caches it in the
  `ticker_intel` table — two users sharing an identifier in the same
  fan-out batch pay for one LLM call, not one each. `search_cache`
  (`(query_hash, trade_date)`) does the same for Tavily queries, closing
  the A1 fan-out fairness gap above. Both get a 90-day cleanup beat task
  (`cache_tasks.py`).
  - **Hard type boundary, not a discipline, keeps per-user data out of the
    shared cache**: `l1_identifiers_for_user` is the only channel from a
    user's own (per-user-judged) anomaly list into L1 — its return type is
    `list[str]`, identifiers only. Every numeric fact then comes from
    `window_data.resolve_global_moves`/`HoldingMove`, which is global by
    construction. This shape exists because the first draft read numbers
    straight out of the per-user-weighted `PriceAnomaly` structure, and
    three independent review rounds each found a different per-user value
    (a threshold-derived `trigger` field, then value-weighted
    price/pct fields from theme-merged anomalies, then a theme-slug-keyed
    news lookup) that had leaked into the shared cache — auditing fields
    one at a time was losing that race. The same rule applies to any
    future cross-user shared-cache consumer (A3's `macro_event_intel`,
    A4): selection may be per-user, values must not be.
  - **L1 describes exactly one trading day, never a report window**: an
    earlier version scoped L1's price-move fact to the calling user's own
    `[period_start, period_end]` (`period_start = user_watermark(user)`,
    per-user by construction) — two users analyzing the same identifier on
    the same day could get different windows, and whichever `generate_report`
    call reached L1 first cached its own window's numbers for everyone else
    that day. Fixed by dropping the window concept entirely:
    `window_data.day_window_bounds(trade_date)` is a pure function of the
    date only.
  - **Compliance scan runs on stripped output, matching Pass 2**:
    `_generate` calls `_strip_markers` (same as Pass 2's
    `cleaned = _strip_markers(raw_body)`) before `_scan_forbidden_output` —
    without it, a model-emitted disclaimer line could false-trip the scan
    and permanently blacklist that identifier's cache slot for the day.
  - **A headline-only candidate (no captured close yet, e.g. a pre-market
    manual run) is skipped entirely, not cached**: caching it would lock
    that identifier's slot for the whole trading day — even after the real
    close is captured later (e.g. the scheduled `after_close` batch), a
    cache hit would keep serving the earlier, unsupported-by-data version.
  - `LOW_COST_LLM_MODEL` is used with `data_collection=deny` kept enforced
    (no BYOK exception — L1 identifiers are holdings-derived, unlike Pass
    1's public-only inputs) under the default provider pin
    (`OPENROUTER_PROVIDER_ORDER`) — confirmed via a real OpenRouter call
    with these exact parameters that this alias is actually served under
    deny, not just assumed.
  - Full 7-round review history and design rationale:
    `Docs/Ring 1-A design.md` §4.3/§4.8 (Obsidian).

### L2 shared macro-event cache (Ring 1 stage A3, issue #128)

The second shared-analysis layer, same shape as A2's L1 but keyed on EVENTS
instead of identifiers: `macro_event_intel.get_l2_intel_batch` computes one
LLM inference per `(event_key, trade_date, prompt_version)` — "what is this
event, and which asset classes/sectors does it bear on" — and caches it in
the `macro_event_intel` table, so a macro theme or scheduled release that
shows up in three users' reports is reasoned about once. Same 90-day beat
cleanup as A2 (`cache_tasks.py`, extended rather than duplicated). Like A2,
**A3 does not change report content**: `ctx.macro_event_intel` /
`ctx.macro_event_exposure` are stored on `report_inputs` but never fed to
Pass 2 — A4 is the consumer (design doc §1.2).

- **Two event vocabularies, one table, prefixed keys**: `theme:<name>` (a
  `macro_detector` ThemeHit, keyword table `config/macro_keywords.yml`) and
  `fwd:<forward_events.id>` (a scheduled calendar row, already uniquely
  keyed by `uq_forward_events_key`). `load_forward_events` now returns `id`
  so the events A3 analyzes and the events §2.5 renders come from one
  loader, never two divergent queries.
- **The A2 type-boundary rule is applied harder here**:
  `l2_event_keys_for_user(session, trade_date, macro_signals) -> list[str]`
  is the only channel from per-user state into the cache (`ctx.macro_signals`
  IS per-user — `detect_macro_signals` runs over
  `load_news_window(..., user_id)`, so both the theme set and its backing
  articles depend on that user's watermark and `news_surfaced` ledger).
  `build_l2_facts(session, event_keys, trade_date)` then takes a Session,
  plain strings and a date — there is no parameter through which a
  watermark, portfolio or anomaly list COULD arrive; it re-derives theme
  evidence itself from `load_day_news`. Same rule as `ticker_intel.py`
  states for A3/A4: selection may be per-user, values must not be.
- **Day-scoped by construction** (A2's round-5 lesson): nothing here reads
  `period_start`/`period_end`. Theme evidence is one ET calendar day's
  global news; a forward event's facts are its immutable calendar row.
- **Closed-enum output, validated before storage**: the model picks
  `affected_asset_classes` from `VALID_ASSET_CLASSES`
  (`asset_class_config.py`) and `affected_sectors` from
  `sector_taxonomy.VALID_SECTORS` minus `OTHER` (`OTHER` is the bucket an
  UNCLASSIFIABLE holding falls into, so accepting it would sweep every
  unknown-sector holding into the event's exposure). Out-of-taxonomy labels
  are dropped and logged — an invented synonym would not error, it would
  intersect with nothing and turn a real exposure into a silent miss.
  `VALID_SECTORS` is derived from `_YF_SECTOR_MAP`'s values, not
  hand-listed, so it cannot drift from `map_yf_sector`'s actual output.
- **`sector` scope is NOT widened**: `affected_sectors` is stored for the
  forward-event holding-relevance mapping that already runs on `sector`
  (`report_sections._forward_exposure`) — this repo's one sanctioned use.
  The per-user exposure step (`user_event_exposure`) reads asset_class only,
  locked by a test.
- **Per-user half costs nothing**: `user_event_exposure` intersects the
  cached classes with the user's own `portfolio_summary["by_asset_class"]`
  keys. Pure set arithmetic, zero LLM calls (design doc §5.3).
- **Failure/compliance handling mirrors L1 exactly**: output is
  `_strip_markers`'d before `_scan_forbidden_output` (so a model-emitted
  disclaimer can't blacklist the day's only slot for that event); a
  violation, an API failure, or unparseable JSON writes a null-analysis
  marker row so the event is attempted at most once per day rather than once
  per user; a candidate with NO global facts is skipped without calling the
  LLM and **without writing any row at all** (an "attempted" marker would
  itself lock out a later, better-informed run the same day).
- **Daily cap fairness — TWO budgets, one per event kind**
  (`_MAX_L2_THEME_ANALYSES_PER_DAY = 10`, `_MAX_L2_FORWARD_ANALYSES_PER_DAY
  = 15`, counted separately by key prefix). Ordering alone is not enough and
  the first draft got this wrong: candidates are ordered deterministically
  and globally (sorted themes, then forward events by scheduled date —
  unlike `l1_identifiers_for_user`, which deliberately keeps the caller's
  own |move| order), but the ORDER is only stable within one user's list,
  and the lists themselves differ, because `theme:` keys are per-user while
  the `fwd:` calendar is global. Under one shared cap, the day's first
  non-quiet user could therefore be a user with no theme hits, spend the
  entire budget on calendar events, and leave every later user's themes
  unanalyzed until tomorrow (caught by blacktomb42 review round 1 on PR
  #157). Separate budgets make that impossible; truncation WITHIN a kind
  (earnings season filling the forward budget) is a genuine cost ceiling
  that truncates the same global list for everyone, not a fairness defect.
  Still NOT closed in general: a cap that binds over genuinely disjoint
  per-user candidates — L1's situation — needs batch-level orchestration and
  belongs to A4.

### Personalized assembly + fan-out budget fairness (Ring 1 stage A4, issue #128)

The consumer the first three checkpoints were built for. When
`SHARED_COMPUTE_ENABLED` is on, the §2/§3/§4 body is ASSEMBLED from the L1/L2
analyses (`report_assembly.py`) instead of inferred from scratch by one giant
per-user Pass 2 — that skipped `PRIMARY_LLM_MODEL` call is the cost
reduction, and the shape becomes `O(|identifier union|) + O(N)`.

- **The saving is the narrowed task, not a smaller model.** The assembly
  prompt never carries the raw news corpus or search snippets — those were
  already digested into L1/L2 — so `build_assembly_prompt` has no
  `news_items`/`search_results` parameter at all. Re-adding one would
  silently restore Pass 2's token profile while looking like a feature.
- **The type boundary is INVERTED from A2/A3's, because the risk is.** A2/A3
  had to keep per-user values OUT of a shared cache; A4 writes no cache — it
  READS two and mixes in per-user holdings, so its failure mode is another
  user's shared rows landing in this user's report (CLAUDE.md's §1.3
  cross-user leak, at the last checkpoint). `build_assembly_prompt` therefore
  takes **no `Session`** and `report_assembly.py` imports **no ORM model**:
  with no DB handle it cannot ask for "everything cached today", only for
  what the per-user caller passes — and that (`ctx.ticker_intel`,
  `ctx.macro_event_intel`) is already scoped by `l1_identifiers_for_user` /
  `l2_event_keys_for_user`. Locked by structural tests plus a real
  three-user fan-out assertion (`test_shared_compute_a4.py`), not by review
  attention.
- **Degradation is the default, and that is the whole safety story.**
  `_try_assembly` returns `None` — meaning "fall back to Pass 2" — for every
  failure mode: switch off, `ASSEMBLY_LLM_MODEL` unset, both caches empty,
  provider error, or a body failing the same completeness guard. The worst
  case of enabling A4 is the pre-A4 report, never a thinner one. Pass 2 still
  RAISES on a truncated body (nothing left to fall back to); assembly falls
  back in the same run. `body_is_incomplete` (`report_prompts.py`) is the one
  expression of that rule so the two cannot drift.
- **`ASSEMBLY_LLM_MODEL` is deliberately empty by default** — it is an
  OUTPUT of the shadow comparison, not an input. Enabling the switch without
  it falls back rather than guessing a model whose quality on this task
  nobody has measured.
- **Shadow comparison** (`ASSEMBLY_SHADOW_MODELS`, comma-separated): runs the
  assembly pass once per listed model over the SAME prompt, stores results in
  `report_inputs["assembly_shadow"]`, ships and emails nothing. Run it with
  `SHARED_COMPUTE_ENABLED=false` and one round yields both comparisons at
  once — architecture (shipped Pass 2 body vs each assembled body) and model
  selection — with costs read off `report_inputs["llm_calls"]`. Exception-
  isolated: a measurement harness must not be able to fail what it measures.
- **`regenerate_report(mode="analyze")` re-runs the pass that WROTE the
  body**, keyed on the stored `body_source`. Re-running Pass 2 on an
  assembled report would not just be the wrong pass — it would write
  `pass2_raw` while leaving the superseded `assembly_raw` in place, and since
  that key wins in `assembly_raw or pass2_raw`, the next `mode=render` would
  silently rebuild the OLD report. `mode="render"` stays token-free for both
  sources; pre-A4 rows carry neither key (`ReportInputsDict` is
  `total=False`) and resolve to `pass2_raw` unchanged.
- **Fan-out budget fairness, finally solved generally** (`shared_budget.py`).
  The same bug surfaced once per checkpoint — A1's Tavily budget, A2's L1
  cap, A3's L2 cap — because a shared capped daily resource consumed
  sequentially in a never-rotating user order (`active_user_ids` is sorted)
  starves the same users every day. A3's per-event-kind split only worked
  because L2 candidates group on a key prefix; L1's are per-user by nature,
  so it handed the general problem forward. The rule: **allocate from what is
  actually LEFT, divided by how many users still have to be served
  (including this one)** — `fair_share_budget(remaining, users_remaining)`,
  threaded as `generate_report(users_remaining=len(user_ids) - index)` into
  both `get_l1_intel_batch` and `get_l2_intel_batch` (which slices each of
  its per-kind budgets independently, preserving A3's split). No user can
  starve a later one; unused share flows forward because the divisor shrinks;
  `users_remaining=1` (every pre-A4 call site) means no restriction. It
  decides HOW MANY, never WHICH — candidate ordering stays per-user by
  design. Deliberately NOT a reservation table or a round-robin merge of all
  users' candidate lists: those need every user's candidates derived before
  any user's report is generated, a whole extra pass whose only product is an
  ordering.
- **"User investment context" (design doc §6.3) is scoped to the portfolio
  snapshot** — weights, concentration flags, asset-class and currency mix.
  There is no user-profile/risk-tolerance model in this codebase (that is
  Stage B), and A4 does not invent one.
- **A3's `sector` boundary holds**: the assembly path reads `asset_class`
  exposure only (`user_event_exposure`); `sector` stays scoped to the
  forward-event mapping in `report_sections._forward_exposure`.

### L3 day-level cross-name synthesis (Ring 1 quality gate, issue #128, PR #167)

The gap A1–A4 left open: L1 (per identifier) and L2 (per event) structurally
cannot express "these identifiers moved together today for one mechanism" —
three overlay comparisons on a real 26-holding book showed assembly was
otherwise as deep as Pass 2 per-name, but never produced that cross-name
sentence, because nothing in the input shape could. `cross_name_intel.py`
adds a third shared layer that performs exactly that join, once per trading
day for the whole system.

- **No per-user selection channel at all, unlike L1/L2.** What this layer
  analyzes ("every identifier the system briefed today") is already a global
  fact, readable straight from `ticker_intel`/`macro_event_intel` — so
  `get_day_synthesis(session, trade_date, ...)` has no parameter a
  watermark, portfolio, or anomaly list could arrive through. The per-user
  narrowing happens entirely on the way OUT, via `clusters_for_user`.
- **Output is decomposable clusters, not a day-level paragraph, because that
  shape is a leak-prevention property, not a formatting choice.** A summary
  naming everything analyzed today could not be narrowed to one user's
  book — it would carry other users' holdings into this report as prose no
  matter how the identifier list beside it were filtered. So:
  `clusters: [{identifiers, mechanism, summary, confidence}]`, with the
  summary required to describe the mechanism and name no identifiers.
  `clusters_for_user` additionally drops any cluster whose summary NAMES an
  identifier the reader does not hold (a prompt rule is an instruction, not
  a guarantee) — checked against the FULL day's briefed-identifier universe
  (`day_briefed_identifiers`), not just a cluster's own filtered members,
  and expanded through `holding_news.load_entity_aliases()` — a genuine
  entity/company-name subset, deliberately NOT the full
  `holding_news_keywords.yml` recall table, which mixes in theme/tech tokens
  ("gold", "lithography", "Nasdaq") a legitimate mechanism summary is
  expected to use.
- **Cache key carries an `input_fingerprint`** (sha256 over the day's
  global L1 identifier set), not just `(trade_date, prompt_version)` — a
  date-only key would freeze the day's conclusion to whichever user's
  `generate_report` reached it first, and every later user would read a
  conclusion that structurally cannot mention any of their names (the same
  "early write locks the day" shape L1's headline-only path hit once).
- **`_MAX_SYNTHESES_PER_DAY` (9) is 3x `_MAX_ATTEMPTS_PER_KEY` (3), not
  equal to it** — the first version had them equal, so one non-retryable
  failure or compliance block on the day's first fingerprint could write
  `attempt_count=3` in a single shot, zeroing the entire daily budget for
  every later, genuinely different fingerprint that day. Widening the
  constant was the accepted fix over splitting `attempt_count`'s dual
  meaning (cost tracking vs daily-budget accounting) — both concrete ways to
  split it reopen a version of the bug issue #160 already closed (a new
  flag column either lands on all three sibling tables or splits their
  semantics; capping on distinct-fingerprint/success count instead of
  `SUM(attempt_count)` lets a bad-provider day retry unboundedly across an
  ever-changing fingerprint stream). Full tradeoff write-up: Obsidian
  `Hermes/Portfonia/Docs/Ring 1-A design.md` §6.7.
- **L1's own prompt (`l1-v4`) dropped its macro-brief channel entirely,
  rather than reading L2 through a global loader.** An earlier draft passed
  `ctx.macro_event_intel` (a per-user L2 SELECTION) into L1 facts as
  `macro_briefs`, baking a per-user selection outcome into a value written
  to the shared `ticker_intel` cache — the round-5 window-leak shape in a
  new field. L3 already performs the L1+L2 join globally, so the fix was
  removing the join from L1 rather than reading L2 without contamination a
  second time.
- **`ASSEMBLY_PROMPT_VERSION` = `a4-v2`** (bumped from `a4-v1` alongside the
  new CROSS-NAME MECHANISM block, closed-set TRANSMISSION labels, TRACKING
  POSITION display rules — sub-1%-weight holdings get one line, never a
  heading, but are NOT floored out of L1 selection; deliberate legal
  tracking-position use case — and a TECHNICAL POSITION block).
- **Status**: merged (squash `c308e6c`, PR #167), three independent review
  rounds (2 bugs / 7 suggestions / 2 nits, all verified and fixed).
  `SHARED_COMPUTE_ENABLED` stays **false** — this closes the quality-gate
  structural gap, it does not itself authorize switching the production
  body-source; that is a separate, later decision.

### Narrative-layer redesign: Pass 2 material widening for large no-anomaly holdings (Ring 1 quality gate, issue #128, PR #168)

The product owner rejected assembly (L1-recap style) as the primary send
path after a 2026-08-17 side-by-side compare (26-holding book): assembly's
TSM section — 22.5% of the portfolio, +1.22% on the day but below its own
anomaly threshold — had only a moving-average line and "shares an AI capex
mechanism with other holdings"; Pass 2's own section for the same holding
had quarterly revenue, customer names, CEO commentary, and a full
Anthropic-demand -> advanced-node -> TSM transmission chain. Root cause:
Pass 2's depth comes from seeing raw search/news text and being allowed to
use public industry structure (which is what "grounded" material genuinely
supports) — assembly's design deliberately withholds both. Direction set:
**material sharing, not narrative sharing** — L1/L2/L3 stay as shared
caches, but every user's report body is still written by their own real
Pass 2 call, now fed richer material. `SHARED_COMPUTE_ENABLED` is
unaffected either way; full design rationale and iteration history: Obsidian
`Hermes/Portfonia/Docs/Ring 1-A Narrative Layer Redesign (Quality Gate Reversal).md`.

- **Large no-anomaly holdings get material too** (`large_weight_identifiers`
  in `ticker_intel.py`, top-5 by weight ≥5%, identifier strings only — same
  type-boundary discipline as every other L1 selection channel). Pass 2's
  own material-gathering in `report_generator.py` used to look only at
  `ctx.price_anomalies`; it now unions `anomaly_ids | weight_ids`, so a
  holding large enough to matter no longer needs to cross an anomaly
  threshold to get recalled news + a targeted search.
- **Weight-targeted search queries are date-locked to the report window**
  (`_targeted_weight_queries`/`_rank_title_matches_first`,
  `report_search.py`) — an unqualified `"{ident} stock news catalyst"` query
  pulled generic, sometimes months-stale articles in an early compare. The
  date lock is now enforced two ways: the query text embeds the window
  (`"{ident} stock news catalyst {start} to {end}"`, still the
  `search_cache` key) AND `_run_tavily_search`/`_fetch_one_query` accept an
  optional `date_windows: dict[str, tuple[date, date]]` param that maps to
  Tavily's real `start_date`/`end_date` publish-date filter — the query text
  alone was never enough, since Tavily itself was never told to restrict by
  date (PR #168 round 2 review). Every other caller (Pass 1, anomaly-targeted
  search, L1 leftover top-up) passes no `date_windows` and is unaffected.
- **`_weighted_identifiers` aggregates by identifier before ranking** — a
  position split across lots (this product preserves upload order, so the
  same ticker can legitimately appear as more than one `Holding` row; VOO is
  the worked example) used to be ranked as separate half-sized rows, letting
  one identifier occupy two `top_k` slots and evict a genuinely distinct 5th
  holding. `l1_identifiers_for_user`'s weight channel now calls
  `large_weight_identifiers` directly instead of re-slicing the same data
  inline, so the two call sites can't drift.
- **Combined targeted-search budget now respects `fair_share_budget`**
  (`report_generator.py`) — the anomaly + weight-targeted Tavily budget used
  to spend the full remaining daily allowance with no
  `fair_share_budget(remaining, users_remaining)` division, unlike every
  other shared-budget consumer in the same function (L1/L2/L3, and the
  leftover top-up right below it). This call runs earlier in a fan-out
  batch, so it could exhaust the day's budget before any later user — or
  even that top-up — got a turn.
- **`NAMING IS NOT ANALYSIS` no longer contradicts `GROUNDED CONNECTIONS
  ONLY`** (`report_prompts.py`, both part of `_SHARED_BODY_RULES`). The
  former told the model to write a causal chain whenever "a holding sits on
  the chain that development would transmit through" — a judgment the model
  itself would make; the latter forbids exactly that ("a plausible-sounding
  mechanism you construct yourself... is not grounding"). Rewritten to
  require the SUPPLIED MATERIAL state the exposure, and to explicitly defer
  to `GROUNDED CONNECTIONS ONLY` by name. Also fixed the `LARGE HOLDINGS
  WINDOW PRICE` reference leaking into assembly's system prompt —
  `build_assembly_prompt` never renders that section (no
  `large_holding_moves` parameter at all), so `_ASSEMBLY_SYSTEM` was
  pointing the model at data that, for that consumer, never exists.
  `_rule_direction_requires_evidence`/`_rule_naming_is_not_analysis` are now
  parameterized functions; `_SHARED_BODY_RULES_NO_LARGE_HOLDINGS` (imported
  by `report_assembly.py` instead of `_SHARED_BODY_RULES`) composes the
  no-large-holdings variant of both — duplicating only the composition
  wiring, not the rule prose, for the six rules unaffected either way.
- **Status**: merged (squash `1a831a9`, PR #168), two independent review
  rounds on the review-fix commits (1 bug / 1 suggestion / 1 nit, then
  0 bugs / 3 suggestions, plus 1 nit from an independent redundant review
  pass — all verified against actual code and fixed with TDD). Deployed to
  production (`systemd-run docker compose up -d --build`, no new migration,
  `/health` verified, 7 containers stable, clean logs). `SHARED_COMPUTE_ENABLED`
  stays **false** — this closes gaps in Pass 2's own material-gathering, it
  does not touch the assembly path or authorize turning it on.

### System default analysis framework — B1 (Ring 1 stage B, issue #129, PR #172)

The system-wide "house analytical stance" from `Portfonia Concept & Design.md`
§4.3 ("System Default Investment Philosophy") had sat as unimplemented prose since 2026-05-14 — the
real system prompt (`_COMPLIANCE_SYSTEM_PREFIX` + `_SHARED_BODY_RULES`) never
carried any product-specific investment philosophy, only compliance
constraints and structural writing rules. B1 closes that gap.

- **`config/analysis_framework.yml`** (`app/services/analysis_framework.py`
  loader) — English-only prose (reasoning layer, never surfaced to the
  reader), **hot-reloaded on every call** (same contract as
  `asset_class_config.load_asset_class_config`: an edit takes effect on the
  next report, no process restart), fails loudly on a missing file or an
  empty/missing `version`/`text` rather than silently degrading to a
  neutral framework. `version` is written to
  `report_inputs.analysis_framework_version` on every report (audit only —
  the full text is deliberately never stored, to keep it out of any future
  endpoint that reads `report_inputs`). Bilingual review record + the
  product owner's sign-off: Obsidian `Hermes/Portfonia/Analysis Framework
  Philosophy.md`.
- **Injection order is compliance -> framework -> shared body rules**, each
  layer explicitly subordinate to the one before it — `_build_pass2_system()`
  / `_build_assembly_system()` (`report_prompts.py` / `report_assembly.py`)
  compose this fresh on every call. These became **functions, not module
  constants** — the pre-B1 `_PASS2_SYSTEM`/`_ASSEMBLY_SYSTEM` were frozen at
  import time, which would never pick up a config edit in a long-lived
  Celery worker process. Both call the same `load_analysis_framework()` —
  one philosophy, not two hand-copied texts (this module has twice paid for
  that class of drift: PR #117's two CSS strings, PR #157's two
  `_FORWARD_WINDOW_DAYS`).
- **The framework only reallocates attention, never produces a directional
  claim** — it decides what earns space and how a time horizon frames
  significance; a self-limiting clause makes it explicitly subordinate to
  every evidence/directional-claim rule below it in the same prompt. Eight
  numbered items (time horizon, structural-evidence depth, portfolio-shape
  weighting, macro/geopolitical transmission, relevance-not-prevalence,
  condition-change-without-forecast, valuation-as-documented-relationship,
  trace-to-a-named-observable) plus that clause — full text and the
  Concept §4.3 mapping: Obsidian doc above.
- **`v1` -> `v2`** (2026-08-22, same PR cycle): after a real-report overlay
  comparison the product owner tightened items 1/2/3/8 — explicit defaults
  for session-scale price moves, a "trace to structural-position change"
  requirement for item 2, an explicit weight/evidence rebalance self-check
  for item 3, and named example phrases for item 8's "no generic closer"
  rule ("worth watching" etc. banned only standing alone). **A code-level
  automated version of the item-3 weight/evidence check (parse §3, score
  evidence strength, reject-and-retry) was explicitly deferred** — no
  evidence-strength scoring mechanism exists yet; tracked in issue #173.
- **§2 Macro Signals rewrite** (same PR, product owner's explicit ask):
  `_SECTION2_INSTRUCTIONS` (`report_prompts.py`) and the inline §2 block in
  `report_assembly.py` changed from "cover every triggered macro theme
  under a rigid bold 'Impact on this portfolio' sub-heading with forced
  short/medium/long-term sub-bullets" to "select 2-4 themes with genuine
  evidenced change this period, write each as one flowing paragraph, let
  the analysis framework's own judgment decide space/time-horizon framing".
  A theme with no direct, concrete mapping to a held identifier does not
  earn its own §2 paragraph by default — at most an aside inside the
  relevant holding's §3 analysis. **Cross-report repetition avoidance is
  prompt-only today** ("don't restate at the same length report after
  report") — there is no persisted memory of what a previous report
  covered; a real fix needs a ledger analogous to `news_surfaced` (see
  below), tracked in issue #171.
- **`_PROMPT_VERSION` -> `f2-v7`** (`report_generator.py`) — the bump
  comment also documents that `f2-v6` was itself under-documented (PR #168's
  narrative-layer rewrite changed `_SHARED_BODY_RULES` without bumping this
  constant, the same class of gap PR #167 round 3 caught on
  `ASSEMBLY_PROMPT_VERSION`).
- **Provenance**: two rounds of independent code review (blacktomb42) —
  round 1 found 2 real bugs (`ASSEMBLY_PROMPT_VERSION` not bumped for a real
  contract change; the widened tech-breakthrough theme's bare `breakthrough`
  keyword and its Chinese-language counterpart firing on ordinary headlines),
  round 2 (after fixes) found 0 bugs.
  Merged squash `8287dd3`. Deployed to production 2026-08-22.

### Macro keyword theme pool — widened to 17 themes (issue #129 B1 + issue #175)

`config/macro_keywords.yml` grew from the Ring 0 starting set of 8 themes
(`Portfonia Concept & Design.md` §7.1.3) to 17, across two PRs in the same
session — the B1 PR itself (7 new themes: tech breakthroughs, US Treasury,
JGB, China macro, Russia-Ukraine, the East Asia alliance, and G7/global
governance, needed because §2's rewrite above now asks the model to
*select* from the candidate pool rather than mechanically cover every
trigger — the selection is only as good as what it can select from) and a
same-day follow-up (issue #175, PR #174: 2 more new themes — US domestic
politics and China-Japan friction — plus targeted keyword additions to
several existing themes, from a product-owner-reviewed candidate list).

- **Recurring lesson across both PRs, caught by Grok review each time: bare
  generic-word keywords false-fire far more than they look like they would.**
  Every one of these was added, then caught and fixed: bare `breakthrough`
  and its Chinese-language counterpart (fires on routine tech-marketing
  headlines / any generic price-threshold phrase), bare `Europe`/`EU`/`yen`
  (fires on routine Europe-market
  roundups / ordinary Japan-FX copy), bare `sanctions` (fires on virtually
  any sanctions headline), bare `Congress` (collides with "National
  People's Congress" / "Indian National Congress"), bare `PLA` (collides
  with "Project Labor Agreement", a real term in the US construction/
  infrastructure business news this product's own RSS feeds carry). The
  fix pattern is always the same: replace the bare word with a compound,
  context-qualified phrase (`scientific breakthrough`, `EU sanctions`,
  `US Congress`, `PLA drills`/`PLA aircraft`/`PLA Navy`, etc.) — **when
  adding any new keyword to this file, default to a qualified phrase, not
  a bare single word, and ask what unrelated headline it could plausibly
  match before adding it.**
- **A single-token match can also be a *fairness* bug, not just a
  false-positive one**: `macro_event_intel.py`'s `theme_keys` are
  `sorted()`, and the daily L2 analysis cap is consumed in that sorted
  order — an ASCII-named theme (e.g. the G7/global-governance theme) sorts
  before every Chinese-named theme, so a keyword that fires too broadly on that theme
  would systematically win the shared daily L2 budget over genuinely
  rarer themes, not just miscategorize one article. Caught on bare
  `sanctions` in PR #174 round 2 review — worth remembering for any future
  ASCII-named theme.
- **`config/macro_keywords.yml` is not under `locales/`, so its Chinese
  keywords are NOT the Language Policy's carved-out exception** (see
  "Language Policy (MANDATORY)" below) — a real gap the product owner
  caught mid-PR #174. Scoped fix so far: **no new Chinese keywords added**
  to that PR's own two new themes (US domestic politics and China-Japan
  friction are English-only).
  Pre-existing Chinese keywords elsewhere in the file (Ring 0 onward,
  including the whole China A-share-policy theme) are **left as-is for now, a known,
  separately-tracked gap** — and, checked against `news_fetcher.py`'s five
  configured RSS sources (NYT/FT/Reuters-via-Google-News/CNBC/Google News
  Business, all `hl=en-US`), **currently match nothing**: there is no
  Chinese-language source in the capture pipeline today, so none of the
  file's existing Chinese keywords are actually reachable in production —
  this lowers the urgency of a cleanup (nothing regresses today either
  way) but does not make the Language Policy violation acceptable to
  extend further.
- **Provenance**: PR #174, two rounds of independent code review
  (blacktomb42) — round 1 found 2 bugs (bare `Europe`, bare `yen`) + 1
  suggestion (bare `EU`), round 2 (after fixes) found 1 bug (bare
  `sanctions`) + 2 suggestions (bare `Congress`, bare `PLA`), both rounds
  fully fixed and verified. Retroactively tracked as issue #175 (filed
  after implementation — this PR started directly from conversation, a gap
  against the Issue Tracking convention below). Merged squash `57b75c7`.
  Deployed to production 2026-08-22 alongside B1.

### News dedup ledger: closing the window-boundary permanent-miss gap (issue #30)

`load_news_window` (`app/services/window_data.py`) used to select
`News.published_at > start, <= end` — a strict range keyed to the report
watermark. A news item published inside window A but not *ingested* until
after window A's `period_end` fell through BOTH windows: window A never saw
it (not yet in the `news` table when window A ran), and window B excluded it
via the `> start` lower bound (its `published_at` predates window B's
start). Two independent exclusions, zero windows that ever selected it — a
permanent miss, not a delay. Same-day multi-run (manual + scheduled
`session_node`s sharing overlapping-but-distinct watermarks) made the race
more likely, not less.

- **Fix**: `load_news_window` now selects `published_at <= end` with **no
  lower bound at all** — decoupling news selection from the watermark
  entirely, per the original issue's proposed direction. Dedup is delegated
  to a new ledger table, `news_surfaced` (`app/models/news_surfaced.py`,
  migration `f1a2b3c4d5e6`): `(user_id, news_id)` unique + `report_id` +
  `surfaced_at`. Once a news item has appeared in a report of a given
  user's that reaches a DONE status (`success`/`needs_review`/`skipped` —
  the same set `user_watermark()` already uses), it's excluded from every
  future selection **for that user**, regardless of how old its
  `published_at` is.
- **Uniqueness is `(user_id, news_id)`, not `news_id` alone** (PR #139
  review round 1, a real gap in the first draft): `news` is a global
  capture-layer store, but reports are per-user with independent
  watermarks — the same item can legitimately need to surface once for
  each user. A global-only unique key would've meant the second user to
  generate a report never saw an item the first user's report already
  marked. `user_id` is threaded through both `load_news_window` and
  `mark_news_surfaced`.
- **Why a join table, not a `surfaced_at` column on `news` directly**: the
  issue was written 2026-06-20, before ADR-002's per-`session_node`
  watermarks landed. A single timestamp column can't cleanly express "has
  this appeared in any of several independently-watermarked report
  streams" — the join table generalizes without assuming there's only one
  watermark per user.
- **Migration backfills from report history, not schema-only** (PR #139
  review round 1 — the first draft was schema-only and would have deployed
  with an empty ledger): with no lower bound and an empty `news_surfaced`,
  the first production report generated after deploy would have selected
  the ENTIRE historical `news` table (up to 1yr retention) as "unsurfaced",
  poisoning macro-signal detection and quiet-day classification, then
  marked all of it surfaced — including items no user was ever actually
  shown. `f1a2b3c4d5e6` instead reconstructs history from every DONE
  report's stored `report_inputs['news_items']`, hashing each item's `url`
  with a frozen snapshot of `news_fetcher._url_hash` (not live-imported,
  matching this repo's migration-immutability convention) to resolve it
  back to a `news.id`. `failed` reports are skipped (never actually shown).
  This is deliberately NOT "mark everything with `published_at <=
  max(period_end)` as surfaced" — that blanket approach would permanently
  hide late-ingested stragglers that were never shown to anyone, reintroducing
  H-DEBT-3 by a different mechanism.
- **Marking is atomic with the status commit**: `mark_news_surfaced(session,
  user_id, report.id, url_hashes)` is called immediately before
  `session.commit()` at both DONE-status sites in
  `generate_incremental_report` (the quiet-day `skipped` path and the final
  `success`/`needs_review` path) — same transaction, so a report can never
  end up DONE with its news unmarked (or vice versa) from a partial commit.
- **Idempotent against Celery redelivery**: `(user_id, news_id)` is unique
  on `news_surfaced`; `mark_news_surfaced` inserts via
  `ON CONFLICT (user_id, news_id) DO NOTHING`
  (`uq_news_surfaced_user_news`), so a `task_acks_late` redelivery
  re-marking the same window's news is a no-op, not an `IntegrityError`.
- **`generate_report` unmarks on retry** (PR #139 review round 1, the
  second real bug): reopening an existing `needs_review` row for retry
  resets `report_inputs` but reuses the row's frozen `period_start`/
  `period_end` — without unmarking, the retry's `load_news_window` call
  would silently see the first attempt's own marks and select a smaller
  news set for the identical window. `unmark_news_surfaced(session,
  report.id)` runs in `generate_report`'s existing-row reset branch, before
  the pipeline re-fetches. A retry of a `failed` row is an unaffected
  no-op (a `failed` report never reaches a `mark_news_surfaced` call site).
  `regenerate_report` is unaffected either way — it rebuilds from stored
  `report_inputs` without re-fetching (existing #6 contract), never calling
  `load_news_window`/`mark_news_surfaced` at all.
- **ORM/migration index alignment** (PR #139 review round 1 nit): the
  `NewsSurfaced` model declares `index=True` on `report_id`, matching the
  migration's `ix_news_surfaced_report_id` — this repo doesn't otherwise
  mirror every migration-declared index onto the ORM model, but doing so
  here avoids `alembic revision --autogenerate` proposing a spurious drop.
- **Test coverage**: `app/tests/test_window_data.py` — a regression test
  reproduces the exact permanent-miss shape (a "straggler" item that would
  have been dropped by the old lower bound) and asserts it's selected once,
  then never resurfaces after being marked; cross-user isolation (marking
  surfaced for one user doesn't hide an item from another); the
  unmark-on-retry mechanism restores the original candidate set; a
  redelivery test asserts double-marking produces exactly one row, not an
  exception. `app/tests/test_report_generator.py` — a wiring test asserts
  `unmark_news_surfaced` is called with the reopened report's id on a
  `needs_review` retry, and not called on a fresh generation.
  `app/tests/test_migrations_round_trip.py` — seeds a real DONE report + a
  `failed` report (whose inputs must be ignored) + an unrelated news row
  against a real Postgres DB, runs the actual migration, and asserts only
  the DONE report's item resolves to a `news_surfaced` row.
- **Provenance**: two rounds of independent code review (blacktomb42) on
  PR #139 — round 1 (Request changes) found 2 real bugs (empty-ledger
  deploy, needs_review retry) + 2 suggestions/nits (per-user uniqueness,
  ORM/migration index drift), all verified against actual code and fixed;
  round 2 (Approve) found 0 new issues. 516 tests passing (was 511 at
  first review), `ruff format`/`ruff check`/`mypy --strict` clean. Merged
  2026-08-13 (`2946d0a`); not yet deployed to production.

### `report_generator.py` split into modules (issue #37)

`report_generator.py` had grown to 2657 lines mixing prompt construction,
code-built section renderers, the LLM transport, Tavily search, JSONB
serialization, the compliance output scan, translation, and orchestration.
Split, pure refactor (no behavior change — every moved function kept its
exact body, verified by the same test assertions passing before and after):

| Module | Responsibility |
|---|---|
| `app/services/report_context.py` | `ReportContext`/`ReportInputsDict` (the `report_inputs` JSONB shape) |
| `app/services/report_llm.py` | OpenRouter transport: `_openrouter_client`, `_call_llm`, `_BYOK_PROVIDER_ORDER` |
| `app/services/report_serializers.py` | ORM/dataclass → JSONB dict (`_serialize_*`) |
| `app/services/report_search.py` | Tavily search + daily-budget tracking + targeted anomaly queries |
| `app/services/report_prompts.py` | Pass 1 / Pass 2 prompt text (system prompts, `_build_pass1_prompt`/`_build_pass2_prompt`, `_stale_ticker_hint`) |
| `app/services/report_sections.py` | code-built §1/§4.2/§4.4/§2.5/footer/data-window renderers |
| `app/compliance/output_scan.py` | Layer-4 output backstop (`_scan_forbidden_output`, `_strip_markers`, `_strip_body_disclaimer`) — co-located with `forbidden_vocab.py`, not a `report_*` module, since both are the same compliance-scaffolding concern |
| `app/services/report_translation.py` | render-to-output-language pass (`_translate_md`) |
| `app/services/report_generator.py` (stays) | orchestration only — `generate_report`, `regenerate_report`, `_render_full_md`, `_is_short_manual_quiet` |

`report_generator.py` imports from all of the above (one dependency
direction, no cycle) and is still the only module `app/routers/reports.py`
and `app/tasks/report_tasks.py` import `generate_report`/`regenerate_report`
from — `LLMEmptyResponseError` moved with `_call_llm` at the time, so its
import site changed to `app.services.report_llm` (superseded by issue #55,
which moved it again to `app/services/llm_errors.py` — see that section
below for the current home). The old `test_report_generator.py`
(93 tests, 1826 lines) was redistributed to a matching test file per module
(`test_report_context.py`, `test_report_llm.py`, `test_report_serializers.py`,
`test_report_prompts.py`, `test_report_sections.py`, `test_output_scan.py`,
`test_report_translation.py`); `test_report_generator.py` keeps only the
`generate_report`/`regenerate_report` end-to-end tests. `mypy --strict`'s
`no_implicit_reexport` (part of `--strict`) caught one leftover
`rg._BYOK_PROVIDER_ORDER` test reference that only worked because
`report_generator.py` happens to import that name for its own use — fixed to
import the constant directly from its owning module, which is the general
lesson this refactor's design doc (issue #37 comment) called out: don't rely
on a symbol being reachable through another module's unrelated import, reach
for its actual owning module.

**PR #150 review (blacktomb42, Approve, 0 bugs / 3 non-blocking) fixed
`_BYOK_PROVIDER_ORDER`'s home a second time**: the first draft parked it in
`report_translation.py` (Pass 1 in `report_generator.py` then imported it
from that leaf) — the review pointed out it pins BOTH Pass 1 and translation,
so a translation-only home would let a future "translation no longer needs
BYOK" edit silently break Pass 1's hard pin. Moved to `report_llm.py` (next
to `_call_llm`'s deny/`allow_fallbacks` pairing — transport/compliance
policy, not either call site's own concern); both `report_generator.py` and
`report_translation.py` now import it from there. The review also caught a
real transcription gap this refactor's own move-verification missed: a
`limit=126` `Read` call during the test-file split landed exactly at the old
file's second-to-last line, silently dropping the final
`assert "Data Sources & Disclaimer" in report.report_md` from
`test_generate_report_quiet_day_has_footer` — restored, and cross-checked
by diffing every `assert` line across old vs. new test files (235 = 235,
content-identical modulo the `rg.` → per-module alias rename) to rule out
any other chunked-Read truncation. A third finding (stale "…above" wording
in cross-module comments naming symbols that had moved to a different file)
was fixed in `output_scan.py` and, on a proactive re-check for the same
class of staleness, in `report_prompts.py` too (not itself flagged, but the
same bug).

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

### LLM failure taxonomy (issue #55)

`app/services/llm_errors.py` classifies any exception raised by an LLM call
into an `LLMErrorCode`, and each code carries an `ErrorPolicy`
(`retryable` / `fallbackable`). Both LLM call sites branch on that verdict
instead of on concrete SDK exception types.

- **The module owns classification, NOT retry policy.** The two call sites
  have order-of-magnitude different budgets and keep separate loops:
  `_call_llm` (Celery report task, minutes of headroom) uses
  `config/llm_retry.yml`'s backoff sequences; `holding_parser.parse()`
  (interactive upload, 45s SLA, 2 x 20s attempts) must never sleep at all —
  the connection sequence alone (30s+90s) would blow the hard time limit and
  get the worker SIGKILLed. A test (`test_parse_never_sleeps_between_attempts`)
  locks that. Do not "unify" the loops.
- **Classification is by HTTP status, not SDK subclass**
  (`_classify_status`), so an SDK version that stops mapping a status to its
  own subclass still gets the right verdict rather than falling to `UNKNOWN`.
  `UNKNOWN` is deliberately non-retryable — a programming error must not be
  retried as if transient. Note `APITimeoutError` subclasses
  `APIConnectionError`; both are `CONNECTION`.
- **`fallbackable` has no consumer today and must not be given a speculative
  one.** No call site has a second-tier model to escalate to (holdings
  parsing runs one model twice since #84; `_BYOK_PROVIDER_ORDER` is a
  compliance hard pin that by definition must not fall back). It is stored
  because it is half of the classification's meaning, not as scaffolding for
  a fallback orchestrator that does not exist.
- **Five real defects this replaced** (all reproduced as failing tests
  before the fix, none of them theoretical):
  1. `holding_parser` indexed `response.choices[0]` with no empty-choices
     guard. OpenRouter's malformed 200 (`choices=None`, the same fault
     `_call_llm` has guarded since I-DEBT-2) raised a `TypeError` — not an
     `openai.OpenAIError`, so it escaped the retry loop entirely and failed
     the upload on first occurrence.
  2. `holding_parser` parsed JSON *outside* the attempt loop, so a malformed
     body — the single most retry-worthy failure mode — was the only one
     never retried, with the second attempt still unspent.
  3. `holding_parser`'s blanket `except openai.OpenAIError` retried
     non-retryable faults (bad key, malformed request), burning up to 20s of
     a 45s SLA to reach the identical failure.
  4. `_call_llm`'s two per-type counters shared one
     `max(len(a), len(b)) + 1` loop bound, so an alternating run (429,
     connection, 429) exited the loop with `resp` unassigned and died on
     `resp.choices` with a bare `AttributeError`, discarding the real cause.
     Each group now draws from its own budget and the bound is their sum.
  5. Provider 5xx (`APIStatusError`, not `APIConnectionError`) was never
     retried by `_call_llm`, and `LLMEmptyResponseError` was raised *after*
     the loop — classified retryable but escalated straight to the 5-minute
     Celery retry, contradicting its own classification.
- **`LLMEmptyResponseError` moved here from `report_llm.py`** and is
  deliberately NOT re-exported from it — importers reach for
  `app.services.llm_errors` (mypy `--strict`'s `no_implicit_reexport`
  enforces this; same lesson issue #37's split already paid for once).
  It subclasses `LLMCallError(RuntimeError)`, preserving the pre-existing
  contract that `routers/reports.py` / `holdings_tasks.py` branch on
  `RuntimeError`.
- **Extended to `ticker_intel`/`macro_event_intel` by issue #160** — see the
  section below for what the product call turned out to be.

### Bounded retry for the shared intel caches (issue #160)

L1/L2 wrote a null-analysis marker on EVERY failure, and a marker is final
for the rest of the `trade_date`. Correct for a failure an identical call
reproduces; wrong for a transient one — one connection reset during the first
user's report starved every later user in the same fan-out, and every manual
re-run that day, of that key's intel. `attempt_count` (migration
`c5d6e7f8a9b0`, both tables) now bounds attempts by the SYSTEM rather than
locking on the first failure.

- **`_MAX_ATTEMPTS_PER_KEY = 3`** in both modules (initial + 2 retries), a
  product decision: whatever reaches these handlers already survived
  `_call_llm`'s own backoff (up to 30s+90s on a connection fault), so the
  retry only covers a blip that cleared between two users of one fan-out.
  Locked by a test in each module; keep the two values in step.
- **One integer expresses both states, so there is no second "permanent"
  flag column to drift**: a retryable failure (`llm_errors.is_retryable` —
  the #55 taxonomy) records `this_attempt`; a non-retryable one (auth, bad
  request) and a compliance block write `_MAX_ATTEMPTS_PER_KEY` directly and
  lock the key on the spot. L2 additionally treats unparseable JSON as
  retryable (the taxonomy's INVALID_JSON — the model is non-deterministic
  even at temperature 0, and `_parse_l2_response` already spent its free
  no-new-call second chance on the same text).
- **The daily caps now count `SUM(attempt_count)`, not rows**
  (`_attempts_today`, renamed from `_count_analyzed_today`) — otherwise a
  retried key gets its extra attempts free and the ceiling silently loosens
  by a factor of 3 on exactly the day it matters. `_generate` therefore
  returns `(result, budget_charged)` and the batch loop subtracts what was
  actually charged rather than a flat 1 (PR #162 review round 1): a lock
  writes 3 to the row, so a flat decrement made the budget the batch was
  spending and the budget the next caller recomputes from the SUM two
  different quantities. `attempt_count` is best read as "slots consumed from
  this key's allowance", not "HTTP calls made" — a lock consumes the whole
  allowance after one call, which keeps the cap conservative (an upper bound
  on real spend), never permissive.
- **`_write_cache` is an upsert, not `on_conflict_do_nothing`** (a retry must
  raise an existing marker's count, and a retry that succeeds must replace
  the marker with the real analysis), guarded by `where analysis IS NULL` so
  a stored analysis can never be overwritten by a later marker.
- **`_fetch_cached` passes `populate_existing=True`, and that is
  load-bearing**: the whole fan-out shares ONE Session, and the Core upsert
  does not refresh an already-identity-mapped instance — without it the third
  user would re-read the second user's stale row, see one attempt fewer than
  really happened, and keep attempting past the cap. Both modules' cap tests
  drive several callers through a single session for this reason; do not
  "simplify" them into separate sessions.
- **What this deliberately does NOT fix**: there is one scheduled report
  batch per `trade_date` (Mon/Wed/Fri 17:00 ET) and it runs for minutes, so
  an outage longer than the batch loses that day's L1/L2 regardless. Covering
  that needs a delayed re-attempt plus report re-render, which is A4-adjacent
  work, not this mechanism. Note also that as of A3 neither cache feeds Pass
  2 at all (`report_inputs` only), so today a miss costs no report content —
  that changes when A4 lands.

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
- `_call_llm` (`app/services/report_llm.py` — split from `report_generator.py`
  in issue #37) logs model/finish_reason/tokens/cost on every call and warns on
  non-`stop` finish; raises `LLMEmptyResponseError` on empty `choices` and
  retries per the failure taxonomy above (issue #55) with bounded backoff.
  `pin_provider=False` (used only for
  translation) lets OpenRouter route freely instead of restricting to the
  pinned provider order. Backoff sequences are admin-editable via
  `config/llm_retry.yml` (issue #38, `app/services/llm_retry_config.py`),
  loaded fresh on every call — same hot-reload pattern as
  `asset_class_thresholds.yml` (#35, see below). Bounded (300s/wait, 5
  entries/sequence, finite-only) so a config typo can't pin a worker
  indefinitely. `_BYOK_PROVIDER_ORDER` (`app/services/report_llm.py` — the
  Pass 1 + translation DeepSeek pin, issue #78/#79) is deliberately NOT in
  this config — it's a compliance decision, not an operational tuning knob.
- `report_inputs` (JSONB) is written via `ReportContext.to_jsonb()` (still
  `dict[str, Any]` — the ORM column itself is untyped JSONB) but read back
  through `ReportInputsDict` (issue #39, a `TypedDict, total=False` mirroring
  `ReportContext`'s fields): `regenerate_report`/`_tavily_used_today` `cast`
  into it so mypy catches a mismatched key/type at the call site instead of a
  runtime `KeyError`. The two are kept in sync by hand — no automatic
  enforcement — guarded by a test asserting the TypedDict's type hints match
  `ReportContext`'s dataclass fields exactly.
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
  and `sector` is otherwise only populated by `POST /admin/portfolio/refresh`
  (moved from `POST /portfolio/refresh`, removed, in issue #129 checkpoint B2)
  or the scheduled capture tasks.
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
| Local dev | Homebrew PostgreSQL 16 + Redis (native), used only to back `pytest`'s real-Postgres tests — the app itself does not run locally anymore (see System conventions table). Colima for Hermes gateway only. |
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

**Watching is the same problem as launching.** After `systemd-run`, drop
the SSH session. Check progress with short reconnects (`systemctl
is-active` / `systemctl show` / `tail` of an on-server log). Do not hold
`ssh '... while systemctl is-active ...'`, a local monitor whose child is
a long-lived SSH, or `run_in_background` on an SSH that stays connected
for the job — those die with the VPN/TUN drop the same way a foreground
`docker compose up` does. A dropped check is not task failure; reconnect
and read the unit and the log. Redirect the job's stdout to a file on the
server when `journalctl` will not carry the full stream (interactive
Python, `docker compose exec`). This applies to every long production
command (deploy, UAT, one-shot `docker compose exec`), not only
`portfonia-deploy`.

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
  `_BYOK_PROVIDER_ORDER` in `report_llm.py`), the exact provider `deny`
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

## Admin surface: API endpoint first, UI later (MANDATORY)

Any feature with an **administrative purpose** — something only the product
owner uses, not part of a normal user's journey — ships first as an
`/admin/*` API endpoint authenticated by an ops token. A management UI is an
optional layer on top of those endpoints, never a prerequisite for the
capability existing.

- **Status**: implemented (issue #129 Ring 1 stage B, checkpoint B2,
  2026-08-22). `app/routers/admin.py` (`APIRouter(dependencies=[Depends(
  require_ops_token)])`) mounts at `/admin` in `main.py`; `require_ops_token`
  lives in `app/core/deps.py`. `POST /admin/portfolio/refresh` is the first
  real endpoint (moved from the now-removed `POST /portfolio/refresh` —
  decision point 8/11: a global market-data refresh is an ops action, not
  something an individual user should trigger). A structural test
  (`test_all_admin_routes_require_ops_token` in `test_admin_router.py`)
  iterates `app.routes` and asserts every `/admin`-prefixed route's
  dependant chain includes `require_ops_token`, so a future endpoint that
  forgets to opt in fails CI rather than shipping unauthenticated.
- **Auth**: `ADMIN_API_TOKEN` (`Settings`, `SecretStr`, required — no unset
  state, same discipline as `HOLDINGS_ENCRYPTION_KEY`) + optional
  `ADMIN_API_TOKEN_PREV` for a no-downtime rotation window (identical
  double-key pattern to `HOLDINGS_ENCRYPTION_KEY`/`_PREV`,
  `app/core/encryption.py`). Compared via `secrets.compare_digest`, never
  `==` (locked by a test spying on the real function). Header shape:
  `Authorization: Bearer <token>`. Missing/malformed/wrong token → `401`
  (not FastAPI's default 422 for a missing required header — B2's
  acceptance criteria treat "no token" as an auth failure).
- **The ops channel is deliberately NOT the user auth system.** It
  authenticates with a static bearer secret from `.env`, queries no tables,
  and does not depend on the user system existing. The reason is the failure
  mode it has to survive: the channel must still work when the login system
  itself is broken and is the thing being repaired. Hanging it off the same
  auth welds the only repair path to the fault source. (B2 also lands before
  the `users` table exists at all, which makes the separation a structural
  fact rather than a discipline anyone has to remember.)
- **Every `/admin/*` call is audit-logged** (`AdminLoggingRoute` in
  `app/routers/admin.py`, the router's `route_class`) — endpoint, method,
  path/query params, status code, duration, via the existing
  `log_ops_event` (`app/core/ops_log.py`). Never logs the Authorization
  header or token value. A run of 5 consecutive 401s on `/admin/*` fires
  one alert, then resets — a sustained guessing attempt doesn't resend the
  alert on every subsequent request; any 200 resets the counter. **The
  alert is enqueued via `send_admin_alert_task.delay()`
  (`app/tasks/admin_tasks.py`), never called as `send_ops_alert(...)`
  directly from the router** — that function makes a blocking
  `httpx.Client(timeout=15.0)` call, and this is an async request path on
  what may be a single-uvicorn-worker box; a direct call would stall the
  event loop for up to 15s on every 5th unauthorized hit (PR #177 review
  round 3). Route new work needing "do this without blocking the request"
  through the existing Celery queue (every other `send_ops_alert` call
  site in this repo already does) — do not reach for a process-local
  workaround (Starlette `BackgroundTask`, `asyncio.create_task`, etc.)
  before checking whether the queue already covers it.
- **Living endpoint reference**: every implemented and planned `/admin/*`
  endpoint (path, auth, params, curl example) is tracked in Obsidian
  `Hermes/Portfonia/Docs/Ops API Reference.md` — update it in the same
  change that adds/modifies/removes an endpoint, not at stage cleanup.
- Consequence to accept openly: some capabilities will exist with **no user
  interface**, reachable only via curl or an agent calling the endpoint. That
  is the intended tradeoff, not an oversight.

Full design, including token rotation, constant-time comparison, router-level
auth declaration, and audit logging: Obsidian `Hermes/Portfonia/Docs/Ring 1-B design.md` §4.

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
- Integration tests hit a real Postgres (Homebrew Postgres 16 locally, not a
  mock). The whole point is to catch schema/migration drift.
- **Test DB isolation (issues #26/#27, PR #137)**: `session_test_db` creates
  `TEST_DATABASE_NAME` and migrates to head **once per pytest session**.
  `db_session` opens an outer transaction + SAVEPOINT
  (`join_transaction_mode="create_savepoint"`, `autoflush=False` to match
  production). `alembic_cfg` uses a **separate** database
  (`MIGRATION_DB_NAME`) so the revision walk cannot drop the session DB.
  `SessionLocal` is lazy (`get_engine` / `reset_engine`); under pytest it
  raises if `DB_NAME` is not `TEST_DATABASE_NAME` — a forgotten mock must
  fail the test, not write `portfonia_dev`. Celery task tests still mock
  `SessionLocal` (control flow, not SQL).
- **Test DB names are PID-suffixed, not fixed strings (issue #152)**:
  `TEST_DATABASE_NAME` (`app/core/database.py`) and `MIGRATION_DB_NAME`
  (`app/tests/conftest.py`) are `f"portfonia_test_{roundtrip,alembic}_{os.
  getpid()}"`, computed once at import time — not the literal
  `portfonia_test_roundtrip`/`portfonia_test_alembic` PR #137 originally
  used. Development now happens in isolated git worktrees (one per
  task/PR), so two `pytest` invocations against the same local Postgres can
  run concurrently; a fixed name meant one process's session-scoped
  teardown (`DROP DATABASE`) could drop the database out from under the
  other's still-running suite. Two live processes never share a PID, so
  this is collision-free for the only window that matters (concurrent
  runs); a DB orphaned by a hard-killed run just sits under its now-dead
  PID as harmless clutter — no automatic sweep, clean up manually if it
  ever actually accumulates.
- LLM prompt regressions: keep a small fixture of "input portfolio + expected
  shape of output" so prompt edits don't silently violate the layer-3 rule.
- Never let tests touch the developer's real home directory.
- **`caplog` assertions on an already-imported module's logger silently see
  nothing after the session migrate** (first hit 2026-08-13,
  `test_fund_nav_fetcher.py`; still true after #137 — upgrade now runs once
  per session via `session_test_db`, not per test, but that first
  `command.upgrade` is enough): `alembic/env.py` calls
  `fileConfig(config.config_file_name)` with no `disable_existing_loggers=
  False`, so it disables any logger that was already instantiated (e.g. any
  module-level `logger = logging.getLogger(__name__)` from a test's own
  imports) — `caplog.records` ends up empty with no error, which reads as
  "nothing got logged" rather than "the logger got disabled out from under
  the test". Workaround, scoped to the test file (not `alembic.ini`, which
  would be a wider blast radius than this needs):
  `logging.getLogger("your.module").disabled = False` right before the
  `caplog.at_level(...)` block.

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
