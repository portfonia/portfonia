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

## `db_session.commit()` never really commits to Postgres (first hit 2026-09-13, `test_operational_events.py`, issue #446)

`db_session` binds to `connection = get_engine().connect(); outer =
connection.begin()` and joins every `Session` it hands out to that same
connection via a SAVEPOINT (`join_transaction_mode="create_savepoint"`).
Calling `.commit()` on a session built this way only releases the
SAVEPOINT — the OUTER transaction (`outer`) stays open until the fixture
tears down at test end and rolls it back. This is invisible for ordinary
ORM assertions (the same session reads its own uncommitted work fine under
READ COMMITTED), but it is a real trap the moment a test needs a
**second, independent** database connection to see that write — which is
exactly the shape `app/core/operational_events.py`'s writer uses on
purpose (its own short-lived connection, real commits, survives business
rollback — see that module's docstring). A user/row seeded via
`db_session.add(...); db_session.commit()` and then referenced by a
foreign key from an independently-committed write (e.g. `operational_
events.user_id`) fails that FK check — silently, if the writer is
fail-open like this one — because the referenced row does not exist yet
from the other connection's point of view.

**Fix, not a workaround**: seed/verify anything a second connection must
see through a genuinely separate connection, not `db_session` — `with
get_engine().connect() as conn: conn.execute(insert(...)); conn.commit()`.
Symmetrically, verifying the effect of a write made through such an
independent connection can be done via `db_session` itself (it sees
externally-committed data fine under READ COMMITTED) — the trap only runs
one direction: `db_session`'s own writes are the ones invisible elsewhere.
Applies to any future test of an independent-connection writer this
project builds on the same pattern (the issue #446 design note explicitly
expects FX/other capture pipelines to reuse it).
