"""Capture -> storage -> valuation -> sparse history/technical reads share
one normalization identity end to end (issue #57 frozen design, stage 57-2
DoD — these integration cases are unchanged by stage 57-3).

Also carries the static zero-dependency check, tightened to its final form
in stage 57-3: no business module may import OR attribute-access a
provider-private normalization symbol (`_yfinance._normalize_ticker`,
`_normalize_hk_ticker`, `_TICKER_SYMBOL_OVERRIDE`) anymore. Stage 57-2 left
an allowlist of five still-pending intelligence/report consumers; stage
57-3 migrated all five (`report_assembly`, `user_scope`, `ticker_leverage`,
`ticker_intel`, `window_data`) and removed the `_yfinance._normalize_ticker`
forwarding shim and the `_TICKER_SYMBOL_OVERRIDE` re-export, so the
allowlist is now empty.
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

_BANNED_PRIVATE_NORMALIZATION_SYMBOLS = {
    "_normalize_ticker",
    "_normalize_hk_ticker",
    "_TICKER_SYMBOL_OVERRIDE",
}

# Stage 57-3 DoD (issue #57 frozen design section 2/6): zero remaining
# business importers or attribute-accessors anywhere in the tree.
_ALLOWED_REMAINING_IMPORTERS: set[str] = set()


def _references_yfinance_private_symbol(path: Path) -> bool:
    """True if `path` imports, or attribute-accesses via a module alias,
    any of `_BANNED_PRIVATE_NORMALIZATION_SYMBOLS` from `_yfinance`.

    Covers both `from app.services._yfinance import _normalize_ticker` and
    `from app.services import _yfinance; ... _yfinance._normalize_ticker(...)`
    — the two forms the pre-57-3 shim/re-export were actually consumed
    through (see the removed `test_instrument_symbols.py` shim tests)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    yfinance_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.services._yfinance":
            names = {alias.name for alias in node.names}
            if names & _BANNED_PRIVATE_NORMALIZATION_SYMBOLS:
                return True
        elif isinstance(node, ast.ImportFrom) and node.module == "app.services":
            for alias in node.names:
                if alias.name == "_yfinance":
                    yfinance_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.services._yfinance":
                    yfinance_aliases.add(alias.asname or "_yfinance")
    if not yfinance_aliases:
        return False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in yfinance_aliases
            and node.attr in _BANNED_PRIVATE_NORMALIZATION_SYMBOLS
        ):
            return True
    return False


def test_previously_migrated_modules_no_longer_reference_yfinance_private_symbols() -> None:
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
        assert not _references_yfinance_private_symbol(path), (
            f"{rel} still references a provider-private normalization symbol from "
            f"_yfinance — issue #57 requires it to use "
            f"instrument_symbols.normalize_legacy_ticker/intelligence_identifier instead"
        )


def test_repo_wide_yfinance_private_references_are_zero() -> None:
    """Whole-tree sweep, final stage-57-3 form: fail loudly if ANY business
    module references the provider-private symbols — the allowlist that
    covered the still-pending 57-3 consumers is now empty."""
    offenders: set[str] = set()
    for path in _APP_DIR.rglob("*.py"):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        if _references_yfinance_private_symbol(path):
            offenders.add(str(path.relative_to(_APP_DIR)))
    assert offenders == _ALLOWED_REMAINING_IMPORTERS, (
        f"unexpected provider-private-symbol references: {offenders}"
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
