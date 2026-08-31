# Testing infrastructure: full reasoning

Backstory behind CLAUDE.md's Tests section one-liners — why the test DB is
isolated the way it is, and the caplog gotcha's full mechanism. Read this
when test DB isolation misbehaves or a `caplog` assertion silently sees
nothing.

## Test DB isolation (issues #26/#27, PR #137)

`session_test_db` creates `TEST_DATABASE_NAME` and migrates to head **once
per pytest session**. `db_session` opens an outer transaction + SAVEPOINT
(`join_transaction_mode="create_savepoint"`, `autoflush=False` to match
production). `alembic_cfg` uses a **separate** database (`MIGRATION_DB_
NAME`) so the revision walk cannot drop the session DB. `SessionLocal` is
lazy (`get_engine` / `reset_engine`); under pytest it raises if `DB_NAME` is
not `TEST_DATABASE_NAME` — a forgotten mock must fail the test, not write
`portfonia_dev`. Celery task tests still mock `SessionLocal` (control flow,
not SQL).

## Test DB names are PID-suffixed, not fixed strings (issue #152)

`TEST_DATABASE_NAME` (`app/core/database.py`) and `MIGRATION_DB_NAME`
(`app/tests/conftest.py`) are `f"portfonia_test_{roundtrip,alembic}_{os.
getpid()}"`, computed once at import time — not the literal
`portfonia_test_roundtrip`/`portfonia_test_alembic` PR #137 originally used.

**Why**: development now happens in isolated git worktrees (one per
task/PR), so two `pytest` invocations against the same local Postgres can
run concurrently; a fixed name meant one process's session-scoped teardown
(`DROP DATABASE`) could drop the database out from under the other's
still-running suite. Two live processes never share a PID, so this is
collision-free for the only window that matters (concurrent runs); a DB
orphaned by a hard-killed run just sits under its now-dead PID as harmless
clutter — no automatic sweep, clean up manually if it ever actually
accumulates.

## `caplog` sees nothing after the session migrate (first hit 2026-08-13, `test_fund_nav_fetcher.py`)

Still true after PR #137 — the migrate runs once per session via
`session_test_db`, not per test, but that first `command.upgrade` is enough
to trigger this.

**Mechanism**: `alembic/env.py` calls `fileConfig(config.config_file_name)`
with no `disable_existing_loggers=False`, so it disables any logger that
was already instantiated (e.g. any module-level `logger = logging.
getLogger(__name__)` from a test's own imports) — `caplog.records` ends up
empty with no error, which reads as "nothing got logged" rather than "the
logger got disabled out from under the test".

**Workaround**, scoped to the test file (not `alembic.ini`, which would be
a wider blast radius than this needs): `logging.getLogger("your.module").
disabled = False` right before the `caplog.at_level(...)` block.
