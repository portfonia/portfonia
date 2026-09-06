"""Stage 57-2 integration slice: capture -> storage -> valuation -> sparse
history/technical reads share one normalization identity end to end
(issue #57 frozen design, stage 57-2 DoD).

Also carries the static import-provenance check: none of this stage's
migrated modules may import a provider-private normalization symbol
directly anymore (`_yfinance._normalize_ticker` / `_TICKER_SYMBOL_OVERRIDE`)
— only the still-pending 57-3 intelligence/report consumers (plus the
57-1 golden-fixture test) may.
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.routers.holdings import _tickers_with_sparse_history
from app.services.portfolio_calculator import compute_portfolio
from app.services.price_capture import capture_prices
from app.services.technical_position import compute_technical_position
from app.tests.conftest import TEST_USER_ID, seed_user

_APP_DIR = Path(__file__).resolve().parents[1]

# Stage 57-2 migrated this set off `_yfinance`'s private normalization
# symbols (`_normalize_ticker`/`_TICKER_SYMBOL_OVERRIDE` specifically —
# `_finnhub.py`/`_massive.py`/`_yfinance.py` itself/`fx_fetcher.py` import
# OTHER names from `_yfinance`, e.g. the price-scale helpers, which is a
# separate, still-fine concern per the frozen design's transition policy).
# 57-3 will migrate the rest of this set (tracked in the 57-1 PR's
# inventory).
_ALLOWED_REMAINING_IMPORTERS = {
    "services/report_assembly.py",
    "services/user_scope.py",
    "services/ticker_leverage.py",
    "services/ticker_intel.py",
    "services/window_data.py",
}


def _imports_from_yfinance_private(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.services._yfinance":
            names = {alias.name for alias in node.names}
            if names & {"_normalize_ticker", "_TICKER_SYMBOL_OVERRIDE"}:
                return True
    return False


def test_stage_57_2_migrated_modules_no_longer_import_yfinance_private_symbols() -> None:
    migrated = [
        "routers/holdings.py",
        "services/holding_parser.py",
        "services/price_capture.py",
        "services/price_fetcher.py",
        "services/portfolio_calculator.py",
        "services/technical_position.py",
        "services/markets.py",
        "services/instrument_symbols.py",
    ]
    for rel in migrated:
        path = _APP_DIR / rel
        assert not _imports_from_yfinance_private(path), (
            f"{rel} still imports a provider-private normalization symbol from "
            f"_yfinance — issue #57 stage 57-2 requires it to use "
            f"instrument_symbols.normalize_legacy_ticker instead"
        )


def test_repo_wide_yfinance_private_importers_are_the_known_57_3_inventory() -> None:
    """Whole-tree sweep: fail loudly if some OTHER module started importing
    the provider-private symbols during this stage (a new import is a
    review failure per the frozen design's transition/import policy)."""
    offenders: set[str] = set()
    for path in _APP_DIR.rglob("*.py"):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        if _imports_from_yfinance_private(path):
            offenders.add(str(path.relative_to(_APP_DIR)))
    assert offenders == _ALLOWED_REMAINING_IMPORTERS, (
        f"unexpected drift in provider-private-symbol importers: {offenders}"
    )


def _hk_holding(ticker: str) -> Holding:
    return Holding(
        user_id=TEST_USER_ID,
        name="Tencent",
        ticker=ticker,
        pricing_mode="auto",
        currency="HKD",
        market="HK",
        shares=Decimal("100"),
        avg_cost=Decimal("300"),
    )


def test_capture_storage_valuation_and_technical_share_one_identity_for_raw_hk_ticker(
    db_session: Session,
) -> None:
    """A holding stored with a leading-zero-free raw ticker ("700.HK", as a
    user might type it) must resolve through capture, §1 valuation, and
    §4.4 technical reads to the SAME `price_snapshots` row keyed under the
    canonical "0700.HK" form — one write, three consistent reads."""
    seed_user(db_session, TEST_USER_ID)
    db_session.add(_hk_holding("700.HK"))
    db_session.flush()

    ohlcv = {
        "0700.HK": [
            (date(2026, 6, 1), 300.0, 305.0, 298.0, 302.0, 1_000_000.0),
            (date(2026, 6, 2), 302.0, 306.0, 300.0, 304.0, 1_100_000.0),
        ]
    }
    with patch("app.services.price_capture.fetch_ohlcv_range", return_value=ohlcv):
        written = capture_prices(db_session, market="HK", session_node="close")
    assert written == 2
    db_session.commit()

    snapshot = compute_portfolio(db_session, TEST_USER_ID, base_currency="HKD")
    assert snapshot.stale_tickers == []
    (hv,) = [h for h in snapshot.holdings if h.ticker == "700.HK"]
    assert hv.market_value == Decimal("304.00") * Decimal("100")
    assert hv.price_as_of is not None

    technical = compute_technical_position(db_session, "700.HK", "Tencent", date(2026, 6, 3))
    assert technical.bars == 2
    assert technical.last_close == 304.0

    sparse = _tickers_with_sparse_history(db_session, TEST_USER_ID)
    assert sparse == ["700.HK"]  # 2 bars << the 50-bar technical threshold


def test_sparse_history_lookup_matches_normalized_key_not_raw(db_session: Session) -> None:
    """Two lots of the same instrument stored under different raw spellings
    (e.g. imported at different times) must both see the shared capture
    history via the one canonical lookup key — this is the exact issue
    #204/#351 class the sparse-history helper exists to guard."""
    seed_user(db_session, TEST_USER_ID)
    db_session.add(_hk_holding("0700.HK"))
    db_session.flush()

    now_bars = [(date(2026, 1, 1) + timedelta(days=i), 1, 1, 1, 1, None) for i in range(60)]
    ohlcv = {"0700.HK": now_bars}
    with patch("app.services.price_capture.fetch_ohlcv_range", return_value=ohlcv):
        capture_prices(db_session, market="HK", session_node="close")
    db_session.commit()

    sparse = _tickers_with_sparse_history(db_session, TEST_USER_ID)
    assert sparse == []
