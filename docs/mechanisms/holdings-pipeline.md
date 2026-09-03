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
- **Upload-job retention (issue #264, 2026-08-30)**: successful `preview`
  JSONB rows (and all other terminal job rows) are kept 30 days, then
  deleted by the daily `cleanup_upload_jobs` beat task (04:30 ET, staggered
  from the 03:00 backup and the 04:00 shared-intel-cache sweep). Pending
  rows are excluded — `sweep_stale_upload_jobs` owns them.


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

### Accounts table + `holdings.account_id` (issue #129 checkpoint B7)

Normalizes `Holding.broker`/`.account`/`.portfolio` (free-text, encrypted,
in use since Ring 0) into an `accounts` table (`id`, `user_id` FK
`users.id` `ON DELETE RESTRICT`, `UNIQUE (id, user_id)`, `broker` NOT
NULL, `account`/`portfolio` nullable, `archived_at`) plus
`holdings.account_id` (nullable, composite FK `(account_id, user_id) ->
accounts (id, user_id)` `ON DELETE RESTRICT` — see the composite-FK
paragraph below for why single-column wasn't enough). Decision point 5
(Ring 1-B design.md
§9.2/§12.1): **additive, not a migration off the text columns** — the
original `broker`/`account`/`portfolio` columns on `Holding` are kept
unchanged and are still what report §1's broker grouping (rendered
`Custodian`) and `holding_parser.py`'s extraction both read — `account_id`
is additional, never a substitute read path. It exists to give stage C's
inline entry form a stable id to reference.

**Write-path parity is required, not deferred to stage C** (review, PR
#247 round 1 — a real bug in the initial cut): `POST /holdings/confirm`
and `app/scripts/seed.py` are both full-replace writers (delete this
user's holdings, reinsert from scratch) and are the *only* holdings-write
path that exists before stage C's form. Without `account_id` support on
that path, the migration's one-shot backfill goes stale on the very next
confirm — every newly-inserted holding gets `account_id=NULL`, and the
backfilled `accounts` rows become unreferenced ghosts. Both call sites now
go through `app/services/accounts.py::resolve_accounts_for_holdings`,
which reuses the migration's exact grouping rule (decrypted
`(broker, account, portfolio)` tuple) to get-or-create each row's account,
and **archives** (never deletes — `accounts.archived_at` exists for this)
any of the user's accounts no longer referenced after the replace.
Blank/whitespace-only `broker`/`account`/`portfolio` normalize to `None`
before grouping (review, PR #247 round 2) — `report_sections.py` and
`holding_parser._summarize` already treat an empty broker as "Other", and
without this a `broker=""` or padded `" IBKR "` would create a real
`accounts` row disconnected from that rendering. The migration's own
backfill duplicates this normalization inline (a migration must stay a
frozen snapshot, not import a service module that could later drift).

**Currency deliberately stays on `Holding`, not promoted to `accounts`**
(§2.4): the 2026-05 spec's "account = 本位币" assumption doesn't match
reality — a single broker/account routinely holds more than one currency
(the upload preview's `BrokerGroup.subtotals: CurrencySubtotal[]` already
assumes this, e.g. one IBKR account with both USD and HKD lots).

**Migration backfill (`4edf69bf41ab`)**: groups each user's existing
holdings by DECRYPTED `(broker, account, portfolio)` plaintext tuple, not
by the ciphertext columns — Fernet's random IV means two encryptions of
identical plaintext never match (verified against production: every
holding row had a distinct `broker` ciphertext even where several repeated
the same broker name). A holding with a NULL `broker` gets no `accounts`
row and keeps `account_id` NULL — `accounts.broker` is NOT NULL, and
report §1 already buckets broker-less holdings into "Other", so there is
no real institution to normalize such a row against.

**FK ordering note for SQLAlchemy unit-of-work, not just this migration**:
`Holding`/`Report`/`UploadJob`/`NewsSurfaced`/`Account` each carry a
`relationship()` to `User` (and `Holding` one to `Account`) whose sole
purpose is flush-order correctness — SQLAlchemy's ORM only infers
INSERT/UPDATE/DELETE ordering from `relationship()` declarations, not from
bare `ForeignKey()` columns. Without it, `session.add()`-ing a `User` and
a dependent row in the same flush can emit the child's INSERT first and
trip the FK it's declared with — this surfaced as ~180 test failures
across the suite when the FKs below were added, all fixed by either this
relationship fix or (where a fixture wrote under an arbitrary UUID with no
matching `users` row at all, a routine pre-B4 pattern) seeding a real user
row. These relationships are not for query-time navigation — every one is
declared `lazy="raise"` (review, PR #247: an accidental `.user`/
`.account_ref` access now fails loudly instead of emitting a hidden SELECT
— an N+1 risk on any list) and `passive_deletes=True` (a `session.delete()`
must not have the ORM try to load/null relationships and fight the DB's
own RESTRICT, which is the actual enforcement mechanism).

**`holdings.account_id` is a composite FK, not single-column** (review, PR
#247 — closes a real cross-user pointer hole): `(account_id, user_id)
REFERENCES accounts (id, user_id)`, backed by a `UNIQUE (id, user_id)` on
`accounts` (`id` alone is already unique via the PK; this exists purely so
Postgres has a target for the pair). A single-column FK on `account_id`
alone only guarantees the account exists — nothing stops a holding from
pointing at *another user's* account once any writer other than the
per-user migration backfill sets it. Postgres `MATCH SIMPLE` (the default)
skips the composite check entirely when either column is NULL, so
`account_id=NULL` still passes trivially.

### `holdings`/`reports`/`upload_jobs`/`news_surfaced` gain real `user_id` FKs (issue #129 checkpoint B7)

`FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT` added to
all four tables in the same migration as the accounts table above — closing
the gap Ring 1-B design.md §2.2's multi-user audit found: none of these
four had a real FK before this (`user_investment_context` was the only
user-scoped table with one, because it postdates B4 and never carried
legacy pre-FK data). **RESTRICT, not CASCADE**, deliberate: a bare
`DELETE FROM users` must never silently cascade into a user's holdings or
report history. Deletion is `app/services/user_purge.py`'s explicit,
ordered, audited `purge_user()` (issue #199, extended by #225 for Supabase
Auth, extended again here for `accounts`) — see
`docs/mechanisms/admin-surface.md` for the full delete order. Shared
capture-layer tables (`ticker_intel`/`macro_event_intel`/`search_cache`/
`cross_name_intel`) deliberately do NOT get a `user_id` FK here — they
carry no `user_id` column at all by design (stage A's type-boundary
discipline), and B7 does not "helpfully" add one.

Pre-migration safety check (not enforced by the migration itself):
production audited 2026-08-28 — 4 users, 0 orphan `user_id` rows across
all four tables. Re-verify this still holds immediately before running
this migration against production; it assumes the check, it does not
perform it.



### Single-row holdings CRUD, confirm modes, export dialect (issue #92 / #130 C1)

Ring 1 Phase C1 adds an online book that is not file-import-only. The
portfolio dashboard was out of scope for this PR — see the "Portfolio
overview dashboard" section below for C2, added later in a separate PR
(#322). File import stays on `/holdings`; row add/edit/delete/reorder lives on
`/holdings/edit`, `/holdings/new`, and `/holdings/[id]`.

**Write paths (all owner-scoped; another user's id is 404, not 403):**

- `POST /holdings` — one `ParsedRow` body, **no LLM / `holding_parser.parse()`**.
  Locks this user's holding rows (`FOR UPDATE`, plus the user row so an
  empty book still serializes) then `position = max(position)+1`. Classifies
  `asset_class` the same way confirm does. Sector fill is a Celery task
  (`backfill_sectors_task`, commit in the worker) + sparse OHLCV/NAV enqueue
  **only for the new ticker/fund_code** — the request does not wait on
  yfinance. 201 `HoldingOut`.
- `PATCH /holdings/{id}` — partial `HoldingPatch` (no `asset_class`; always
  recomputed). Re-validates the merged row as `ParsedRow` (cash/wmf
  boundary, currency). Untouched EncryptedDecimal money fields are not
  rewritten through float. Re-resolves `account_id` if broker/account/
  portfolio change (`archive_unreferenced=False`). On ticker/fund_code
  change: clears `sector`, `market_price`, `price_as_of`, `price_fetched_at`
  in the same commit, then enqueues sector backfill + sparse capture.
  "Changed" compares what `_apply_write_defaults` actually wrote against
  the pre-patch `holding.ticker`/`fund_code`, not whether the client's
  request body mentioned the field (round 5 fix) — `_apply_write_defaults`
  force-suffixes a ticker server-side even on a PATCH that never touches
  it, e.g. a notes-only edit on a legacy unsuffixed row, so checking the
  request body missed that case entirely and left stale price/sector in
  place with no backfill enqueued.
- `DELETE /holdings/{id}` — that `Holding` row only. Does **not** touch
  `ticker_themes` or anomaly watermarks. 204.
- `PATCH /holdings/reorder` — `{ "ids": [uuid, ...] }` must be a permutation
  of **all** of this user's holding ids else 422. Writes `position` in that
  order. Declared **before** `/{holding_id}` so FastAPI does not treat
  `reorder` as an id.

**`POST /holdings/confirm?mode=append|replace`:** default **append** if
`mode` is omitted (safer than a silent full replace). Frontend always sends
the query param explicitly.

- `append` — insert only, positions from `max+1` in payload order (same
  `FOR UPDATE` lock as POST). Never updates existing rows; duplicate
  ticker+broker is a second lot. Does not archive unreferenced accounts.
  Sector + sparse enqueue only for the new rows. Response is the **full
  book** (so the list UI is not wiped down to the payload).
- `replace` — full delete+reinsert + enqueue whole-book sector backfill
  and sparse capture (not inline yfinance), and `archive_unreferenced=True`.

`Field(ge=0)` on `ParsedRow` still guards shares/avg_cost/current_value for
these new writers too (encryption still prevents a DB CHECK — issue #113).

**Parser preview notes:** `ParsedRow.issues` is `list[IssueNote]`
(`{code, params, severity}`). Preview JSON only — not persisted on confirm.
Only the deterministic postprocess whitelist (`KNOWN_ISSUE_CODES`) is kept;
model-supplied free-text strings and unknown LLM codes are dropped (not
wrapped as `parser_note` `{message}` — zh users would still see English).
Amber highlight is **only** `severity=warning` or `confidence<0.7`.
Deterministic successful transforms (cash amount shares→current_value, drop
spurious ticker on cash, HK/currency normalize) are `info`. Exchange suffix
(`.L` / `.HK` / `.SS` / `.SZ` / `.T`) is **force-applied** once market is
determined (user-set or confidently derived) on file-import confirm,
`POST /holdings`, and `PATCH /holdings/{id}`. When market cannot be
determined the `ticker_no_suffix` warning still fires and no suffix is
guessed. Applying `.L` persists **UK** (PSH is filed as `market=UK` with
ticker `PSH.L`) — UK is a scheduled capture market after #312. Bare-PSH
price lookup still uses the #204 `_TICKER_SYMBOL_OVERRIDE` `PSH → PSH.L`
collision table. Unresolvable listed names persist `market=Other` and
`capture_supported=False`; capture never speculative-yfinances them.
`normalize_ticker_and_currency()` (HK 4-digit canonicalization + suffix-
currency correction) is a single function shared by `_postprocess`'s two
call sites and the router's `_apply_write_defaults`, so file-import and
`POST`/`PATCH /holdings` always store the same canonical ticker for the
same input — before round 5 the router never ran this step, so an API
write of `"700"`+HKD stored `700.HK` while the same input via file-import
stored `0700.HK`, silently missing `ticker_themes`/config-YAML lookups
keyed on the canonical form.

**Market determined but the suffix is ambiguous or unplaceable** (Europe/
Korea have several listing suffixes; an A-share code outside the
recognized digit ranges) is a **separate** issue code,
`ticker_suffix_ambiguous`, from `ticker_no_suffix` (market genuinely
undetermined) — the single old code's copy told users to "set Market"
even when Market was already set. `apply_confirmed_exchange_suffix`
persists the derived market on this branch and marks the row so both
write paths force `capture_supported=False` after `resolve_holding_market`
runs; without that, `resolve_holding_market`'s bare-ticker fallback
(`market_from_ticker` treats any unsuffixed ticker as US) would otherwise
silently stamp a bare EUR/KRW ticker — or an explicit `market=Europe`
holding whose ticker collides with an unrelated US symbol, e.g. `ASML`
(Nasdaq ADR) vs. the intended Euronext listing — as `market=US,
capture_supported=True` and fetch the wrong security. `market="US"` itself
never takes a suffix and must not be swept into this ambiguous
classification (a round-6 fix regression, caught by the full test suite
before it shipped: the first pass would have turned off capture for every
plain US ticker).
Dialect validation rejects surface as `issue_rows`; the dialect path
skips dedup so identical lots survive re-import. cash/wmf with no ticker →
`market=Other`, including when the model inferred A-Share from a mainland
bank broker. Listed auto tickers are **not** reclassified into Other.

**Export / template dialect:** `GET /holdings/export` and
`GET /holdings/template` emit the `#####` comment-rules dialect (one
holding per line, export ordered by `position`). **Locale as of issue
#319 item 9**: an optional `locale` query param (the frontend's current
UI locale, `zh-Hans` mapped to bare `zh`) takes precedence when given;
omitted falls back to `users.locale` (report language) as before — the
two are independently controllable, and this is the only place UI
locale drives anything other than UI chrome. The positional
prefix is name / identifier / currency / shares / avg_cost / broker for
auto-priced listed rows; name / identifier / currency / shares / avg_cost /
current_value / broker when `pricing_mode:manual` — **always all three
numeric slots**, using the placeholder `-` for one that is unset (round 5
fix: emitting a slot only when present let the parser conflate "shares +
current_value, no avg_cost" with "shares + avg_cost, no current_value" —
both are two numeric tokens — and in the worst case fabricate a cost
basis on re-import; `_manual_match_explicit` in `holding_parser.py` reads
this placeholder-marked shape unambiguously, falling back to the older
count-based heuristic only for hand-typed input without the placeholder);
name / current_value / currency / broker for cash/wmf (no cost
basis today). **Trailing tags as of issue #319 item 8**: export now emits
only `account:`, `portfolio:`, `notes:` (quote a value that contains
spaces) — `asset_type:`/`market:`/`pricing_mode:` are dropped, on
product-owner request, as pure classification always re-derivable via
the LLM path. `account`/`portfolio`/`notes` are free-text with no other
slot in the positional dialect, so they keep round-tripping — dropping
them too was PR #321 round 1's caught bug, see the follow-up subsection
below. `price_snapshots` is market data, not what the user paid, so
avg_cost on export is load-bearing (unaffected by the tag change). A
file whose every data line carries at least one surviving tag is still
parsed deterministically (`try_parse_dialect`) and never calls the
LLM — see the follow-up subsection for why that is now the exception,
not the rule, for a typical export. Export `Content-Disposition`
filename is `holdings-YYYYMMDD-HHMMSSZ.md` (UTC); the frontend reads
that header rather than hardcoding `holdings.md`.

**Frontend split:** `/holdings` is upload + parse preview + read-only
current list (not clickable, no drag, no add, no delete). Append/replace
intent is chosen before file selection (issue #319 item 10, see below);
the file preview area then shows one save action reflecting that choice,
still gated by the existing post-parse safety dialogs. `/holdings/edit`
is the full book: native HTML5 drag-reorder (PATCH reorder on drop;
revert to last server order on failure) plus clickable sort-ascending/
descending arrows on the ticker/currency/broker headers reusing the same
`PATCH /holdings/reorder` call (issue #319 item 12); shares/avg_cost
(current_value for cash/wmf)/pricing_mode are inline-editable, each a
single-field `PATCH /holdings/{id}` (issue #319 items 4-5); every other
field, and delete, stay detail-page/dialog-only; click row →
`/holdings/[id]`, add → `/holdings/new`. Forms do not call the LLM.
Submit is disabled while in-flight; success and Back go to
`/holdings/edit` (dirty form confirms before discard). Onboarding
incomplete → banner linking to `/holdings?onboarding=1`; the route is
**not** 404'd during onboarding. Onboarding holdings step stays the
upload page (`mode=onboarding`); Save there uses append so a manual add
is not wiped. Get Started menu: Holdings → `/holdings` only — the
separate "Edit holdings" nav entry was removed (issue #319 item 1; the
`/holdings` page's own card-header button is the sole entry point to
`/holdings/edit`).

### Ring 1-C1 UX follow-ups (issue #319, PR #321)

Twelve direct UX/information-architecture corrections to C1 above, found
by the product owner using the feature for the first time post-merge —
not new features. Full requirements/design: issue #319's two comments,
Obsidian `Hermes/Portfonia/Docs/Ring 1-C design.md` §11.

- **Item 8's tag removal, corrected in PR #321 review round 1
  (blacktomb42)**: the first implementation dropped all six
  `DIALECT_TAG_KEYS` from export, including `account`/`portfolio`/
  `notes` — free-text user data with no other slot anywhere in the
  positional dialect, so that was unrecoverable data loss on #92's only
  rollback path (export `.md` → edit → re-upload), not the "no longer
  free/fast" tradeoff the issue actually discussed and the product owner
  signed off on. Fixed to drop only `asset_type`/`market`/`pricing_mode`.
  `pricing_mode` specifically had to be dropped deliberately, not left
  in "for safety": it is the one tag every `Holding` always has a value
  for, so keeping it would have meant every export line always carries
  at least one tag and the dialect fast path never actually retires in
  practice — silently defeating the accepted tradeoff. Dropping it
  reopened a narrower, separate risk instead: `holding_parser
  .parse_dialect_line`'s manual-vs-auto branch selection used to be
  keyed solely on a `pricing_mode:manual` tag, so a manual-priced row
  reached via a surviving `account`/`portfolio`/`notes` tag would
  silently misparse as 2-slot auto (current_value swallowed into the
  broker field, pricing_mode flipped back to auto). Fixed in the same
  round by making `parse_dialect_line` try `_manual_match_explicit`
  (the placeholder-marked 3-slot shape, "unambiguous by construction"
  per its own docstring) positionally, before any tag check — safe
  regardless of which tags survive on export.
- **Item 10 shipped as a deliberate partial implementation** of "move
  both `appendHint` and `replaceConfirmBody` earlier": only the
  append/replace *choice* moved before the file picker (a `confirmMode`
  toggle, non-destructive, no dialog needed at that point). The existing
  post-parse safety dialogs — the issues-discard confirm for append, and
  the real-parsed-count replace confirmation — are unchanged and still
  gate the actual destructive save. Collapsing the replace confirmation
  into a pre-parse prompt too would only ever be able to show `n=0`
  (no file parsed yet), which is less safe than keeping the real,
  parsed-row-aware one. `POST /holdings/upload` itself does not carry a
  `mode` param — the parse step is mode-independent, so there is no LLM
  cost to save by attaching one.
- Items 1-7, 9, 11-12 shipped matching the frozen design as written; see
  PR #321 and the issue #319 implementation comment for the full list.
- **Item 10's remaining gap closed by issue #323/PR #327**: a new
  `replaceHint` callout renders next to the mode selector (mirroring
  `appendHint`'s placement, mutually exclusive with it) the moment
  Replace is chosen, before any file is picked. It is deliberately
  mode-agnostic and count-free — it never claims a row number, since no
  file has been parsed yet at that point. The post-parse
  `replaceConfirmBody` dialog (real parsed/issue row count) is unchanged
  and remains the actual destructive-action gate; the new callout is an
  earlier heads-up, not a replacement for it. Issue #323's second item
  (zh-Hant `fieldPortfolio` translation) stayed deferred, per the issue's
  own note that a full native-speaker review pass should fix the whole
  catalog at once rather than one key at a time — the new `replaceHint`
  key was still added to all three locale catalogs to keep the
  structural-sync test (issue #209) passing, with zh-Hant mirroring
  zh-Hans verbatim like every other not-yet-reviewed key in that file.


### Portfolio overview dashboard — C2 (issue #320 / #130 C2, PR #322)

`/portfolio` reads `GET /portfolio/summary` (`compute_portfolio()` in
`portfolio_calculator.py`), which already computed most of this for the
report pipeline — C2 is mostly exposing existing calculator output plus one
real gap (P&L) that was never wired up.

**New aggregates, same exclusion gate as the existing `by_market`:**
`by_group` keys on `Holding.portfolio` (`None`/empty → `"Ungrouped"`),
`by_account` keys on `Holding.broker` (`None`/empty → `"Other"`, matching
`report_sections.py`'s §1 Custodian fallback literal). **Deliberately not**
the normalized `accounts` table — `resolve_accounts_for_holdings` dedups
`Account` rows on the `(broker, account, portfolio)` triple, so an `Account`
row is really a broker×account×group combination, not an independent
account entity; reading `account_id` for this breakdown would fold the
group dimension back into account and produce a chart entangled with, and
unreadable independently of, "by group". `account_id`/`accounts` stays at
zero read-paths from this dashboard.

**P&L** (`cost_basis_base`/`unrealized_pnl_base`/`unrealized_pnl_pct`,
both per-holding on `HoldingValueOut` and as snapshot-level totals) is
computed only when `pricing_mode=="auto"` and both `shares` and `avg_cost`
and a valuation are present — `None` (not zero) otherwise, so cash/wmf and
`capture_supported=False` holdings render "—" rather than a fabricated
zero. The snapshot-level totals (`total_cost_basis_base` etc.) sum only
holdings with a computed cost basis — cash/wmf never contributes to either
side of "total unrealized return %".

**Frontend partition for the "no market quote" section**
(`isNoLivePrice()` in `portfolio-helpers.ts`) is
`pricing_mode=="auto" and not capture_supported` — never the broader
`market_value_base is None`, which would also catch a holding with a
transient missing price/FX rate and misclassify a temporary gap as a
permanently-unsupported market. These holdings show only user-entered
fields, participate in no chart or total, and render with the default
`Card` variant — not `variant="urgent"` (that's the issue #269
incomplete-setup-nudge language; this is an informational exclusion
notice, a different speech act — round-1 review finding, PR #322).

`price_as_of_date` is the max captured-close trade date that actually
produced a displayed `market_value_base` this run — not merely matched a
`price_snapshots` row (round-1 shipped the weaker "matched" version; round
2 caught that a snapshot hit for a holding missing `shares`, or one that
fails FX conversion, never reaches a displayed number and must not date
the banner — `used_trade_dates` only appends once `market_value_base is
not None`, PR #322). `None` when nothing was captured, including a
cash-only book — the as-of banner has dedicated copy for that case, since
cash/wmf holdings do have a valuation via `current_value` and a "no priced
holdings" message would contradict a non-zero total assets figure on the
same page (round-1 finding); that None-case copy stays generic rather than
naming cash/wmf specifically, since None also covers an empty book, a
capture-unsupported-only book, or an auto holding still waiting on its
first snapshot (round-2 finding).

`base_currency` widened from a 3-value `Literal` to all 15
`VALID_CURRENCIES` (mirrors `app/schemas/holdings.py`'s frozenset; a
router-level drift-guard test pins the two together). Currency switching
never does client-side FX math — every switch is a fresh
`GET /portfolio/summary?base_currency=X` call, kept genuinely single-flight
by awaiting inside an async `startTransition` scope (a synchronous
`startTransition(() => { void promise.then(...) })` in round 1 made
`isPending` cover only the scheduling call, not the round-trip, so the
switcher stayed clickable mid-fetch and a slower earlier response could
overwrite a later one — round-1 review finding, PR #322); a failed refetch
reverts the switcher to the last successfully-loaded currency rather than
leaving it desynced from the displayed figures.

The by_group/by_account chart legends and the holdings table's
Group/Custodian columns render the same translated "Ungrouped"/"Other"
fallback label so a pie slice can be matched back to its rows — the table
previously showed a bare "—" for these while the chart legend showed the
untranslated English literal (round-1 finding). `BreakdownChart` takes a
`labelFor(key)` prop applied only when building the displayed slice
name, grouping on the untouched raw backend key (`portfolio-helpers.ts`'s
`fallbackOrValue` for the table cells) — round 1 shipped a
`relabelFallbackKey` helper that instead rewrote the source `Record`'s own
keys before charting, so a user's real group/broker named the same as the
translated fallback ("未分组") would silently collapse two backend keys
into one via `Object.fromEntries`, dropping a slice; round 2 deleted that
helper for the `labelFor` prop. by_asset_class used the same
pre-transform pattern (harmless today only because the 13 `asset_class`
translations happen to be unique, not because the pattern is safe) —
round-3 review flagged the inconsistency, switched to `labelFor` too.

### Portfolio overview email — explicit send button (issue #202, PR #329)

Not a formal report and not tied to `confirm_holdings`. Issue #202's
original shape (auto-send an email on every `POST /holdings/confirm`) was
replaced during design review, before implementation started: that
decision predated issue #319/#321's incremental single-row editing, and
"fire unconditionally on every confirm, no dedup" would have meant one
near-duplicate email per single-field save. The fix was not a rate limiter
bolted onto the old trigger — it was removing the trigger. See issue #202's
three pinned comments for the full decision record (original design
contract, the pivot, and the review-driven fixes below).

**Trigger**: `POST /portfolio/send-overview` (`app/routers/portfolio.py`),
authenticated, `base_currency` query param mirroring `GET /portfolio/
summary`'s own param — the emailed total must match whatever currency the
page has selected, not a hardcoded default. Dispatched only from an
explicit "Send holdings overview" button on `/portfolio`
(`send-overview-button.tsx`); `/holdings` and `/holdings/edit` only gained
a plain navigation link to `/portfolio`, no behavior change to confirm.

**Cooldown**: `check_portfolio_overview_cooldown` (`app/core/rate_limit.py`)
claims a 15-minute-TTL Redis key via `set_nx` per user before dispatch —
deliberately NOT built on the existing `_enforce_ip`/`_trip` machinery
every other limiter in that module uses, because that path fires an ops
alert on every trip (right for an abuse signal like resend-verification,
wrong here: a user clicking this button twice in 15 minutes is routine,
not an anomaly). `release_portfolio_overview_cooldown` undoes the claim if
the actual Celery `.delay()` enqueue then fails (review 5100733033) — the
claim is taken before the send is confirmed queued, so without a release
path a broker blip would lock the user out of retrying for the full window
over a message that was never even sent. The response schema
(`SendOverviewResponse`) uses `retry_after_seconds: None` to distinguish
this "enqueue failed" case from a real cooldown for the frontend.

**Email content**: `send_portfolio_overview_email` (`email_sender.py`)
opens its own session (via `send_portfolio_overview_email_task`,
`app/tasks/notification_tasks.py`), resolves the recipient through
`recipient_email_with_purpose` (same fail-closed two-branch handling as
`send_report_email` — an unresolved recipient always alerts ops, at parity,
not downgraded despite this endpoint being click-triggered), and calls
`compute_portfolio()` directly rather than a separate lightweight price
lookup — issue #295 already made that function keep every holding in
`snapshot.holdings` with `market_value_base=None` for an unpriced one, so
this email inherits "list every holding, mark pending, exclude only from
totals" for free. The holdings table is a **new, dedicated, locale-aware
renderer** (`_build_portfolio_overview_markdown`), not a reuse of
`report_sections._build_section1` — that function's table headers are
hardcoded English, translated only by the full report's LLM pass
(`report_generator._translate_md`), which this lightweight, no-LLM email
skips entirely; its own headers come from a new bare `en`/`zh`
`_PORTFOLIO_OVERVIEW_COPY` dict (same shape as `_VERIFICATION_EMAIL_COPY`),
and asset-class labels/placeholders reuse `i18n_glossary.yml`'s existing
`report_glossary` entries via a small `_glossary_term` helper rather than
duplicating those translations a second time. The bilingual disclaimer
footer IS reused as-is via `report_sections._build_footer` (already
locale-independent by design). No `reports` row, no `user_watermark()`
read, no LLM call, no `_scan_forbidden_output` (nothing LLM-generated to
scan).

**Review 5100733033 fixes** (blocker + 3 leftovers, all in PR #329's second
commit): the per-row value cell originally rendered `h.currency`/
`h.market_value` — the holding's OWN currency — which can't be summed to
the `base_currency` total/percent columns next to it and silently ignored
whatever currency the page had switched to; fixed to
`snapshot.base_currency` + `market_value_base`, with a regression test
using an HKD holding (the original all-USD test book couldn't have caught
this). The name cell fell back to nothing when a holding had no `ticker`
(an A-share fund keyed by `fund_code` alone); fixed to fall back to
`fund_code`. The frontend button read the currency switcher's in-flight
`currency` state, which can lead the settled `summary` by one render
during a switch; fixed to pass `summary.base_currency` and disable the
button while a switch is pending. And the cooldown-release fix above.

**Next-report-date**: `next_occurrence_for_cadence` (new,
`app/tasks/__init__.py`, next to `_REPORT_CADENCES`) computes the next Beat
fire time for the user's real `report_cadence` via `crontab.
remaining_estimate` — pinning the crontab's `nowfun` to the passed-in `now`
is load-bearing: `remaining_estimate` measures elapsed time from its own
`nowfun()` call, not from its `last_run_at` argument (that argument only
anchors which past occurrence to search forward from), so an unpinned
crontab silently computes against the real wall clock regardless of what
"now" the caller intended — caught by a test asserting the exact next
weekday/time, not just "some future datetime".

### Portfolio dashboard: by-account breakdown, currency display modes, sector removal (issue #330, PR #332)

Three UX changes to `/portfolio`, scoped in a design-contract comment on
the issue (comments beat the issue summary; GitHub beats the Paperview
vault mirror when they disagree).

**`by_account` renamed to `by_broker`** (`PortfolioSnapshot`/
`PortfolioSummaryResponse`/`compute_portfolio()`, `portfolio_calculator.py`,
`schemas/portfolio.py`, `routers/portfolio.py`) — the field the C2 entry
above describes as `by_account aggregates Holding.broker` was never
per-account, just a custodian rollup; that description is now stale and
superseded by this entry. A genuine `by_account` was added alongside it,
keyed on `h.account or "Other"` (the free-text `Holding.account` column,
not the normalized `accounts` table — same "stays at zero read-paths from
this dashboard" reasoning as the C2 entry's `account_id` argument, since
`Account` rows dedup on `(broker, account, portfolio)` and would fold
dimensions back together). Both fields keep the C2-era exclusion gate
(`market_value_base is not None`) and the same `"Other"` fallback literal,
so `by_broker` and `by_account` can share one frontend constant
(`ACCOUNT_OTHER_KEY`).

**Currency card, three display modes** (本币/native, 归一/normalized,
比例/percentage), computed entirely client-side — no new backend field,
since `HoldingValueOut` already carries native `currency`/`market_value`
and the full holdings list is already in the response. Normalized reuses
`by_currency` unchanged (also the default, so no behavior change for
anyone who never touches the switcher). `nativeCurrencyBreakdown()` and
`currencySharePercentages()` (`portfolio-helpers.ts`) compute the other
two. `BreakdownChart` gained `formatValue`, `showShareOfTotal`,
`showPie`, and `headerControl` props to support a mode-local switcher
without every other caller needing to know about display modes.

Two round-1 review findings (blacktomb42, review 5101430049), both fixed
in the same PR:
- **Pie sizing must share one unit.** The pie's `dataKey="value"` was
  wired straight to whichever record was active, so native mode sized
  arcs from raw mixed-currency numbers — 100,000 JPY would visually
  dwarf 1,000 USD regardless of which was actually worth more. Fixed by
  adding `showPie` (default `true`) and setting it `false` for native
  mode specifically — list-only there, sidestepping the incommensurable-
  units problem rather than trying to size arcs from a second, different
  dataset than the one being labeled.
- **Native-mode membership must match normalized-mode membership.**
  `nativeCurrencyBreakdown()` initially skipped only a holding with a
  null `market_value`; `by_currency` (and thus normalized/percentage
  mode) also requires `market_value_base` to be non-null, so a holding
  with a stale FX rate (native value present, base conversion failed)
  would appear in native mode and vanish in the other two. Fixed to skip
  when either is null, matching `portfolio_calculator.py`'s own gate.

**By-sector (GICS) card removed** from `/portfolio` — UI-only;
`by_sector` stays in `PortfolioSnapshot`/`PortfolioSummaryResponse`/
`portfolio_calculator.py` untouched, since redesigning "by industry" is a
deferred, separate future issue, not blocked by this one.

No drill-down (flat cards only, confirmed in the issue's own out-of-scope
list); `Holding.account_id` untouched.

### Portfolio snapshot export: xlsx and md (issue #331, PR #335)

`GET /portfolio/export?format=xlsx|md&base_currency=<currency>&locale=<locale>`
(`routers/portfolio.py`) — a separate, read-only sibling of `/holdings/export`
(issue #92/#310), not a parameter on it. `/holdings/export` writes the
*declared, unpriced* fields in the `#####` re-import dialect; this exports
the *computed, priced* results from `compute_portfolio()` — same
computation `/portfolio/summary` already runs, serialized differently, no
new aggregation. Design contract from the issue's first comment, followed
without deviation.

**Scope**: one row per `HoldingValue`, columns fixed at
`ticker`/`fund_code`/`name`/`market`/`broker`/`account`/`portfolio`/
`asset_class`/`currency`/`shares`/`avg_cost`/`market_value`/
`market_value_base`/`cost_basis_base`/`unrealized_pnl_base`/
`unrealized_pnl_pct`/`pricing_mode`/`capture_supported`
(`portfolio_export.EXPORT_COLUMNS`, identical order in both formats).
Deliberately omits `sector` (dropped from this dashboard's vocabulary per
issue #330), `notes`/`holding_id` (internal), and every `by_*` aggregate
— those are page-view stats, never read by this module. `as_of`
(`price_as_of_date`) and `base_currency` are a metadata header above the
table in both formats, never a data column.

**New module `backend/app/services/portfolio_export.py`**, not a shared
renderer with `holdings_export.py`: `render_portfolio_export_xlsx()` uses
`openpyxl` (single sheet, two metadata rows, blank row, header row, one
row per holding); `render_portfolio_export_md()` is a zero-dependency GFM
pipe-table builder with its own pipe/backslash/newline escaping — sharing
`holdings_export.py`'s `#####`-dialect renderer was explicitly ruled out
in the design contract, since that renderer serves a different purpose
(re-import) with a different tag/escaping scheme. `openpyxl==3.1.5` was
already pinned in `requirements.txt`, unused until this PR (most likely a
transitive pin from the pandas ecosystem) — no new dependency line was
needed, only `openpyxl.*` added to `pyproject.toml`'s
`mypy.overrides.ignore_missing_imports` (the package ships no
`py.typed`).

**Locale**: column headers switch via `_HEADERS_BY_LOCALE`
(`en`/`zh`, unrecognized falls back to `en`) — a standalone dict, not
imported from `holdings_export.py` (different key set, different
purpose). `locale` query param overrides `users.locale`, same precedence
as `/holdings/export`/`/holdings/template` (issue #319 item 9); resolved
by a private `_export_locale()` in `routers/portfolio.py` that mirrors
`holdings.py`'s `_report_locale()` rather than importing it — this
repo's established convention is one small locale-fallback helper per
export module (see also `email_verification.py`'s `_resolve_locale()`),
not a shared abstraction.

**Frontend**: `ExportPortfolioButtons` (two buttons, "Download .xlsx" /
"Download .md") on `/portfolio`, next to `SendOverviewButton`, using the
same `exportPortfolio()` → `downloadFile()` pattern as the holdings-manager
export button. New `exportXlsxButton`/`exportMdButton`/`exportError` keys
added to all three locale catalogs (en/zh-Hans/zh-Hant).

**Review fix (blacktomb42, review 5103601953, fixed in `666fd61`)**:
`compute_portfolio()`'s `_ratio()` (`portfolio_calculator.py`) stores
`unrealized_pnl_pct` as a 0..1 fraction, not a percent — every other
consumer (the `/portfolio` table's `formatPercent`, the overview email's
`:.1%`) multiplies by 100 before display. The first version of this
export serialized the raw Decimal under a `%`-labeled column, a
systematic 100x understatement for the Excel/LLM consumers this feature
targets; the unit test fixture had masked the bug by hardcoding the
percent-scale value directly instead of the ratio `compute_portfolio()`
actually produces. Fixed by scaling `unrealized_pnl_pct` by 100 in
`portfolio_export._row_values()` (the one shared helper both `render_*`
functions call, so both formats get the fix identically) — with a
regression test that seeds the real backend scale and asserts the
exported value matches what the page displays.
