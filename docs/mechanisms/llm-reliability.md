# LLM failure taxonomy, bounded retry, and reliability mechanisms

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


