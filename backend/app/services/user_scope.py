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

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.user import User
from app.services._yfinance import _normalize_hk_ticker


def active_user_ids(session: Session) -> list[uuid.UUID]:
    """Active accounts that already have holdings, sorted for fan-out.

    Identity comes from `users` (Stage B). Fan-out still requires at least
    one holding so a brand-new signup does not get an empty Pass 2 / email
    on the next scheduled batch.
    """
    rows = (
        session.execute(
            select(User.id).where(
                User.status == "active",
                exists().where(Holding.user_id == User.id),
            )
        )
        .scalars()
        .all()
    )
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
