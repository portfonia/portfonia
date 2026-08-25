# News dedup ledger

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


