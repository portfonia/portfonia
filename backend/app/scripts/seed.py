"""Seed Ring 0 developer's sample holdings.

Idempotent: deletes any rows for DEV_USER_ID first, then inserts five fixtures
covering US stock, HK stock, A-share, China public fund, and a manual-mode
cash entry.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import delete

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import Holding
from app.services.accounts import resolve_accounts_for_holdings


def build_fixtures(user_id: uuid.UUID) -> list[Holding]:
    return [
        Holding(
            user_id=user_id,
            name="Apple Inc.",
            pricing_mode="auto",
            ticker="AAPL",
            currency="USD",
            shares=Decimal("10"),
            avg_cost=Decimal("180.00"),
            asset_type="stock",
            broker="IBKR",
        ),
        Holding(
            user_id=user_id,
            name="Tencent Holdings",
            pricing_mode="auto",
            ticker="0700.HK",
            currency="HKD",
            shares=Decimal("100"),
            avg_cost=Decimal("320.50"),
            asset_type="stock",
            broker="Futu",
        ),
        Holding(
            user_id=user_id,
            name="Kweichow Moutai",
            pricing_mode="auto",
            ticker="600519.SS",
            currency="CNY",
            shares=Decimal("10"),
            avg_cost=Decimal("1680.00"),
            asset_type="stock",
            broker="China Securities",
        ),
        Holding(
            user_id=user_id,
            name="E Fund Blue Chip Select",
            pricing_mode="auto",
            fund_code="005827",
            currency="CNY",
            shares=Decimal("5000"),
            avg_cost=Decimal("2.4500"),
            asset_type="fund",
            broker="Tiantian Fund",
        ),
        Holding(
            user_id=user_id,
            name="USD Cash Reserve",
            pricing_mode="manual",
            currency="USD",
            current_value=Decimal("15000.00"),
            asset_type="cash",
            broker="Schwab",
        ),
    ]


def main() -> None:
    settings = get_settings()
    user_id = uuid.UUID(settings.DEV_USER_ID)

    with SessionLocal() as session:
        session.execute(delete(Holding).where(Holding.user_id == user_id))
        fixtures = build_fixtures(user_id)
        # issue #129 B7 review: same full-replace shape as /holdings/confirm.
        account_ids = resolve_accounts_for_holdings(
            session, user_id, [(h.broker, h.account, h.portfolio) for h in fixtures]
        )
        for holding, account_id in zip(fixtures, account_ids, strict=True):
            holding.account_id = account_id
        session.add_all(fixtures)
        session.commit()

    print(f"[OK] seeded 5 sample holdings for dev user {user_id}")


if __name__ == "__main__":
    main()
