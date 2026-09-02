"""Multi-user scope helpers for the shared-compute layer (issue #128, Ring 1 A1).

`Holding.user_id` (app/models/holding.py) is a bare UUID column — no
ForeignKey, and not on the Fernet-encrypted field list (see CLAUDE.md's
"Holdings encryption at rest") — so "the system's active users" can be read
directly off `holdings` without a `users` table existing yet (design doc
§1.5, Hermes/Portfonia/Docs/Ring 1-A design.md). Once Stage B adds a real
`User` table, `active_user_ids` becomes a one-line swap to query it instead
of `holdings` — not a refactor of anything that calls it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.user import User
from app.services._yfinance import _normalize_ticker

# Cadences whose fan-out still requires at least one holding row (issue #191
# decision point 2). Not in this set -> an empty book still enters the batch,
# so a cadence's "needs holdings?" answer lives in one place instead of an
# `if cadence == "mwf"` scattered wherever this gets checked.
_HOLDINGS_GATED_CADENCES = frozenset({"mwf"})


def _active_user_conditions(cadence: str) -> list[Any]:
    """WHERE clauses shared by `active_user_ids` and `active_users` — kept
    in exactly one place so the two queries can never silently drift apart
    on which users qualify (issue #308)."""
    conditions: list[Any] = [
        User.status == "active",
        User.report_cadence == cadence,
        or_(User.email_verified_at.isnot(None), User.delivery_email_verified_at.isnot(None)),
    ]
    if cadence in _HOLDINGS_GATED_CADENCES:
        conditions.append(exists().where(Holding.user_id == User.id))
    return conditions


def active_user_ids(session: Session, cadence: str) -> list[uuid.UUID]:
    """Active accounts on the given `report_cadence`, sorted for fan-out.

    Identity comes from `users` (Stage B). `mwf` still requires at least one
    holding so a brand-new signup does not get an empty Pass 2 / email on the
    next scheduled batch; `weekly` does not (issue #221 §8 / #191) — an
    empty-book weekly user gets the empty-table content contract instead of
    never being scheduled at all.

    Independent of the cadence-scoped holdings gate (issue #276 Layer 1):
    a user with no verified address — neither `email_verified_at` nor
    `delivery_email_verified_at` — has nowhere a generated report can be
    delivered, so they are excluded from EVERY cadence's fan-out rather
    than paying for generation that can never send. Not scoped to
    `_HOLDINGS_GATED_CADENCES`: unlike the empty-holdings case this is not
    a per-cadence content tradeoff, it is undeliverable regardless of
    cadence.
    """
    rows = session.execute(select(User.id).where(*_active_user_conditions(cadence))).scalars().all()
    return sorted(rows)


def active_users(session: Session, cadence: str) -> list[User]:
    """Same population as `active_user_ids`, but the full `User` row for
    each (issue #308) — `generate_incremental_report`'s fan-out needs each
    recipient's own `locale` (report language) alongside their id. Returns
    full ORM objects sorted by id, not a second per-user lookup after the
    fact: an earlier version of this fan-out re-fetched each user's row
    inside the per-user loop via `session.get(User, user_id)` (or a second
    batched `select`) on the SAME session `generate_report` itself is
    already reading and writing through — reproduced empirically, that
    interleaved second read hung indefinitely under the real-session test
    pattern (`SessionLocal` rebound to the test's own `db_session` fixture,
    e.g. test_weekly_cadence_fanout.py/test_shared_compute_a1.py). Fetching
    every row ONCE, before any `generate_report` call touches this session,
    avoids the interleaving entirely.
    """
    rows = session.execute(select(User).where(*_active_user_conditions(cadence))).scalars().all()
    return sorted(rows, key=lambda u: u.id)


def report_language_for(session: Session, user_id: uuid.UUID, default: str) -> str:
    """One user's own report language (issue #308), falling back to
    `default` if their row can't be found.

    Single shared implementation for every call site that needs ONE user's
    locale (as opposed to `active_users`' whole-batch fetch, which is a
    deliberately different pattern for the fan-out — see its docstring):
    `routers/reports.py`'s self-service generate/regenerate, and
    `email_sender.send_report_email`'s subject/unsubscribe-footer language
    (added after the product owner caught, reading the #308 diff, that
    those two still read the global default directly while the report BODY
    had already switched to the per-user value — a user who picked English
    could get an English body with a Chinese subject and footer).
    `email_sender.py` is a service module and must not import from
    `routers/`, which is why this lives here rather than staying a private
    helper in `reports.py`.

    `default` is an explicit parameter, not `get_settings().OUTPUT_LANG`
    resolved internally: each caller already has its own `settings` in
    scope (fetched via ITS OWN module-local `get_settings` import), and a
    hidden second `get_settings()` call from this module would silently
    stop responding to a caller's `@patch("app.services.<caller>.
    get_settings")` — that exact gap broke several `email_sender.py` tests
    the first time this was written with an internal `get_settings()`
    call. Pass `settings.OUTPUT_LANG` (whatever `settings` the caller
    already resolved) in explicitly instead.

    In production, every caller of this function already resolved a real,
    active `users` row before reaching here (`current_principal` for the
    two `reports.py` call sites; `recipient_email_with_purpose` for
    `send_report_email`), so the fallback branch is unreachable there — it
    only matters for test fixtures that exercise a missing/unmocked row.
    The `isinstance` check (not just `user is not None`) additionally
    guards against a mock session whose `.get()` returns a stand-in object
    with a non-string `.locale` — real rows can never fail this given
    `users.locale`'s `NOT NULL` + `CheckConstraint`, so this changes no
    production behavior, only which fallback a loosely-configured test
    double exercises.
    """
    user = session.get(User, user_id)
    if user is not None and isinstance(user.locale, str):
        return user.locale
    return default


def user_holdings(session: Session, user_id: uuid.UUID) -> list[Holding]:
    """All holding rows belonging to one user."""
    return list(session.execute(select(Holding).where(Holding.user_id == user_id)).scalars().all())


def global_identifier_universe(session: Session) -> dict[str, list[Holding]]:
    """identifier (ticker/fund_code, HK-normalized, uppercased) -> every
    Holding row across EVERY user that carries it.

    Scoped to auto-priced holdings only (`pricing_mode == "auto"`) — a
    manually-priced holding has no quotable price series for L0/L1 to compute
    over, matching the existing filter in `price_capture._market_tickers` and
    the pre-A1 `detect_window_anomalies` holdings query.

    Ticker/fund_code are Fernet ciphertext at rest (issue #31): SQL-level
    DISTINCT/equality on them is meaningless, so this fetches full rows and
    groups by the decrypted value in Python — the same pattern
    `price_capture._market_tickers` already uses, not a SELECT DISTINCT.
    """
    holdings: Sequence[Holding] = (
        session.execute(
            select(Holding).where(
                (Holding.ticker.is_not(None)) | (Holding.fund_code.is_not(None)),
                Holding.pricing_mode == "auto",
            )
        )
        .scalars()
        .all()
    )
    universe: dict[str, list[Holding]] = {}
    for h in holdings:
        raw = h.ticker or h.fund_code
        if not raw:
            continue
        identifier = _normalize_ticker(raw).upper()
        universe.setdefault(identifier, []).append(h)
    return universe
