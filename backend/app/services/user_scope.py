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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.services._yfinance import _normalize_hk_ticker


def active_user_ids(session: Session) -> list[uuid.UUID]:
    """Every distinct user_id with at least one holding row.

    This IS the Stage-A definition of "the system's active users" (design
    doc §1.5) — not a placeholder pending a `users` table. `user_id` is not
    Fernet-encrypted, so a SQL-level DISTINCT is valid here (unlike
    ticker/fund_code — see `global_identifier_universe`). Sorted for
    deterministic fan-out order in logs/tests.
    """
    rows = session.execute(select(Holding.user_id).distinct()).scalars().all()
    return sorted(rows)


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
        identifier = _normalize_hk_ticker(raw).upper()
        universe.setdefault(identifier, []).append(h)
    return universe
