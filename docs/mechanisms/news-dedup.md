# News dedup ledger

### Collection origins and headline records (issue #620)

`news` remains globally unique by `url_hash`. RSS rows use `origin=pool`;
per-instrument rows use `origin=instrument`. Both `load_day_news` and
`load_news_window` select only pool rows, preserving the existing report feed.
An instrument fetch conflicting with a pool hash links the existing row rather
than changing its origin or inserting a duplicate. In the reverse order, an
RSS fetch promotes an existing instrument row to pool while preserving its
record, label and instrument links. `store_headline` returns `(id, inserted)`
from RETURNING; conflicts and promotions report `inserted=False`.
`news_instruments` is unique by `(news_id, identifier)` and cascades when news expires.

Each row stores a closed v1 JSON `record` with `v`, `kind`, `title`, `summary`,
`published_at`, `collected_at`, `label` and `filing_form`. Text is HTML-stripped
and whitespace-normalized; summaries are capped at 500 characters. The database
stores neither URLs nor publisher metadata. Readers reconstruct `NewsItem`
with empty URL/source, while serialization preserves `url_hash`. The #61
render-resume path marks that stored hash surfaced, falling back to hashing
`url` for older saved inputs. Report prompts omit empty source labels.

The daily sweep deletes news older than 30 days. Issue #639 explicitly deletes
`news_surfaced` rows for the expiring news before deleting that news, using the
same cutoff and transaction; `news_surfaced_deleted` reports the count. Instrument
links cascade on news deletion. No surfaced-ledger foreign key is added. This replaces the previous one-year news retention;
price retention remains unchanged. Run evidence is retained for 90 days.
Migration `d62000000001` applies the same 30-day cutoff and irreversibly discards
expired news and retained URL/source columns. Downgrade deletes instrument
rows before reconstructing pool titles and summaries but restores empty
URL/source values. Deployment and migration require
separate owner authorization and a current-day backup, with #621 and #622.
Issue #622 removes report-time search. Instrument-linked news is read through
the same per-user surfaced ledger and every recalled hash is marked together
with the terminal report status. Historical `report_inputs` URL/source keys
are scrubbed by the #622 migration in batches of 500; the downgrade is a
no-op because the data is not recoverable.

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

- **Current selection (issue #639)**: both `load_news_window` and
  `load_instrument_news_by_identifier` select
  `start - LATE_INGEST_WINDOW < published_at <= end` and exclude this user's
  surfaced ledger. The shared `LATE_INGEST_WINDOW = timedelta(hours=48)` also
  controls instrument collection's lookback. This preserves late-ingestion
  recovery for the preceding 48 hours while excluding older, unseen headlines.
  At 49 hours before start an item is excluded; at 47 hours it is eligible;
  the exact 48-hour boundary is excluded. The ledger remains unique by
  `(user_id, news_id)` with `report_id` and `surfaced_at`. Once an item appears
  in a DONE report (`success`/`needs_review`/`skipped`), it is excluded from
  subsequent selection for that user.
- **Seven-calendar-day window floor (issue #611)**: newly computed report
  windows start at the later of the latest completed report end and ET midnight
  seven ET calendar dates before the batch time. Without history they use the
  same floor. Signup backfills with this cutoff; `generate_report` backfills
  exactly when its newly computed start equals the floor, including after a
  gap. It marks news published strictly before the cutoff as surfaced for that
  user before loading news; an item exactly at the cutoff remains eligible.
  Normal Weekly and Mon/Wed/Fri windows keep their previous end and receive no
  backfill; unsurfaced news within 48 hours before that end still loads (#639).
  The seven-day cap takes priority over recovering a failed week's content.
  Retries with a stored window reuse it without recomputing or backfilling;
  regenerations continue using stored inputs. The bounded late-ingestion selector preserves
  normal ledger marking semantics; no migration or retroactive
  report regeneration is involved.
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
  2026-08-13 (`2946d0a`); deployed to production (confirmed an ancestor of
  the 2026-08-25 production deploy, `bf74971`).

