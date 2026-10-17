"""Tests for the issue #788 one-off asset_class reclassification script."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.user import User
from app.scripts.reclassify_asset_class import reclassify_asset_class
from app.services import holding_parser as holding_parser_module
from app.tests.conftest import seed_user

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000d1")


def _holding(**kw: object) -> Holding:
    data: dict[str, object] = dict(
        user_id=_USER,
        name="Fund",
        pricing_mode="auto",
        currency="USD",
        shares=Decimal("10"),
        asset_type="etf",
        asset_class="EQUITY_BROAD",
        market="US",
    )
    data.update(kw)
    return Holding(**data)


def test_reclassify_dry_run_then_apply(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holding_parser_module._parse_ticker_asset_class.cache_clear()
    mapping = tmp_path / "ticker_asset_class.yml"
    mapping.write_text("ticker_asset_class:\n  MAPD1: EQUITY_DM\n  MAPD2: EQUITY_DM\n")
    monkeypatch.setattr(holding_parser_module, "_get_ticker_asset_class_path", lambda: mapping)
    user = seed_user(db_session, _USER)
    version = user.holdings_version
    stale = _holding(ticker="MAPD1")
    stock = _holding(ticker="MAPD2", asset_type="stock", asset_class="STOCK")
    correct = _holding(ticker="MAPD2", asset_class="EQUITY_DM")
    db_session.add_all([stale, stock, correct])
    db_session.flush()

    assert reclassify_asset_class(db_session, apply_changes=False) == 1
    db_session.refresh(stale)
    assert stale.asset_class == "EQUITY_BROAD"

    assert reclassify_asset_class(db_session, apply_changes=True) == 1
    db_session.flush()
    db_session.refresh(stale)
    assert stale.asset_class == "EQUITY_DM"
    assert stock.asset_class == "STOCK" and correct.asset_class == "EQUITY_DM"
    assert db_session.get(User, _USER).holdings_version == version  # type: ignore[union-attr]
    holding_parser_module._parse_ticker_asset_class.cache_clear()
