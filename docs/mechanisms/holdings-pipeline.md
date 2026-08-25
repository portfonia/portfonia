# Holdings pipeline: upload, encryption, constraints, cash/wmf

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


