"""Integration tests for portfolio_calculator — real Postgres, no network."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.services.portfolio_calculator import _CURRENCY_TO_FX_PAIR, _to_base, compute_portfolio
from app.tests.conftest import seed_user

_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_USER = uuid.UUID("00000000-0000-0000-0000-000000000002")
_FX_DATE = date(2026, 1, 2)


@pytest.fixture(autouse=True)
def _seed_users(db_session: Session) -> None:
    seed_user(db_session, _USER)
    seed_user(db_session, _OTHER_USER)


def _seed_fx(session: Session) -> None:
    session.add_all(
        [
            FxRate(pair="USDCNY", rate=Decimal("7.0"), rate_date=_FX_DATE, source="test"),
            FxRate(pair="USDHKD", rate=Decimal("8.0"), rate_date=_FX_DATE, source="test"),
            FxRate(pair="USDCNH", rate=Decimal("7.1"), rate_date=_FX_DATE, source="test"),
        ]
    )


def _stock(
    name: str,
    ticker: str | None,
    currency: str,
    shares: str | None,
    price: str | None,
    *,
    asset_type: str = "stock",
    asset_class: str = "STOCK",
    sector: str | None = None,
    fund_code: str | None = None,
) -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        pricing_mode="auto",
        ticker=ticker,
        fund_code=fund_code,
        currency=currency,
        shares=Decimal(shares) if shares is not None else None,
        market_price=Decimal(price) if price is not None else None,
        asset_type=asset_type,
        asset_class=asset_class,
        sector=sector,
    )


def _cash(name: str, currency: str, value: str, *, asset_class: str = "CASH_EQUIV") -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        pricing_mode="manual",
        currency=currency,
        current_value=Decimal(value),
        asset_type="cash",
        asset_class=asset_class,
    )


def test_full_snapshot_values_and_distributions(db_session: Session) -> None:
    _seed_fx(db_session)
    db_session.add_all(
        [
            _stock("Apple", "AAPL", "USD", "10", "300", sector="Technology"),
            _stock("Moutai", "600519.SS", "CNY", "10", "1400", sector="Consumer Staples"),
            _cash("USD Cash", "USD", "5000"),
            _stock("Broken", "BAD", "USD", None, None),  # missing price → stale
        ]
    )
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.fx_date == _FX_DATE
    assert snap.stale_tickers == ["BAD"]
    # 3000 USD + 14000 CNY/7 = 2000 USD + 5000 cash
    assert snap.total_base == Decimal("10000.00")
    assert snap.by_currency == {"USD": Decimal("8000.00"), "CNY": Decimal("2000.00")}
    assert snap.by_asset_type == {"stock": Decimal("5000.00"), "cash": Decimal("5000.00")}
    assert snap.by_market == {
        "US": Decimal("3000.00"),
        "A-Share": Decimal("2000.00"),
        "Other": Decimal("5000.00"),
    }
    # cash is excluded from the equity sector chart
    assert snap.by_sector == {
        "Technology": Decimal("3000.00"),
        "Consumer Staples": Decimal("2000.00"),
    }
    # asset_class has no "Other" fallback — every holding (incl. cash) lands
    # in a real bucket, this is what §1/distribution/§4.1 read.
    assert snap.by_asset_class == {
        "STOCK": Decimal("5000.00"),
        "CASH_EQUIV": Decimal("5000.00"),
    }


def test_declared_market_overrides_derivation(db_session: Session) -> None:
    """A user-declared market (e.g. USD cash in an IBKR/US account) wins over
    the ticker-derived bucket — cash must land in US, not the 'Other' default."""
    _seed_fx(db_session)
    cash = _cash("USD Cash", "USD", "5000")
    cash.market = "US"  # declared via the .md market column
    db_session.add_all([_stock("Apple", "AAPL", "USD", "10", "300"), cash])
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.by_market == {"US": Decimal("8000.00")}
    assert {hv.name: hv.market for hv in snap.holdings}["USD Cash"] == "US"


def test_concentration_flags(db_session: Session) -> None:
    """Single-holding thresholds depend on the top holding's own asset_class
    (STOCK is tight: >10%/>20%); the asset_class bucket (not raw per-row
    ranking) drives the top-asset-class check, with no "Other" fallback."""
    _seed_fx(db_session)
    db_session.add_all(
        [
            _stock("Apple", "AAPL", "USD", "10", "300", sector="Technology"),
            _stock("Moutai", "600519.SS", "CNY", "10", "1400", sector="Consumer Staples"),
            _cash("USD Cash", "USD", "1000"),
        ]
    )
    db_session.flush()

    c = compute_portfolio(db_session, user_id=_USER, base_currency="USD").concentration

    assert c.top_holding_name == "Apple"  # 3000 > 2000 > 1000
    assert c.top_holding_ratio == Decimal("0.5000")  # 3000 / 6000
    assert c.top_holding_asset_class == "STOCK"
    assert c.single_holding_watch is True  # 0.50 > STOCK watch 0.10
    assert c.single_holding_high is True  # 0.50 > STOCK high 0.20
    assert c.top3_ratio == Decimal("1.0000")
    assert c.top3_watch is True  # >0.50
    assert c.top_asset_class_name == "STOCK"  # Apple + Moutai = 5000 > cash 1000
    assert c.top_asset_class_ratio == Decimal("0.8333")  # 5000 / 6000
    assert c.asset_class_watch is True  # 0.8333 > 0.50
    assert c.asset_class_high is True  # 0.8333 > 0.65


def test_concentration_thresholds_loosen_for_broad_index_top_holding(db_session: Session) -> None:
    """A broad-index ETF carries a wider single-holding threshold than a
    single stock at the same weight, since it is already diversified."""
    _seed_fx(db_session)
    vanguard = _stock(
        "Vanguard S&P 500",
        "VOO",
        "USD",
        "10",
        "300",
        asset_class="EQUITY_US_BROAD",
    )
    # Grok review round 3 (PR #322): the two holdings are exactly tied in
    # value (3000 == 3000). compute_portfolio() now orders holdings by
    # position (matching /holdings book order) before concentration ranks
    # them, so an explicit position — not incidental insertion/scan order —
    # is what breaks the tie deterministically.
    vanguard.position = 0
    cash = _cash("USD Cash", "USD", "3000")
    cash.position = 1
    db_session.add_all([vanguard, cash])
    db_session.flush()

    c = compute_portfolio(db_session, user_id=_USER, base_currency="USD").concentration

    assert c.top_holding_name == "Vanguard S&P 500"
    assert c.top_holding_ratio == Decimal("0.5000")
    assert c.top_holding_asset_class == "EQUITY_US_BROAD"
    assert c.single_holding_watch is True  # 0.50 > EQUITY_US_BROAD watch 0.30
    assert c.single_holding_high is True  # 0.50 > EQUITY_US_BROAD high 0.45


def test_unclassified_stock_sector_defaults_to_other(db_session: Session) -> None:
    _seed_fx(db_session)
    db_session.add(_stock("HK Co", "0700.HK", "HKD", "100", "80", sector=None))
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert "Other" in snap.by_sector
    assert snap.by_sector["Other"] == Decimal("1000.00")  # 100*80 HKD / 8


def test_base_currency_cny(db_session: Session) -> None:
    _seed_fx(db_session)
    db_session.add(_stock("Apple", "AAPL", "USD", "10", "300", sector="Technology"))
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="CNY")

    assert snap.total_base == Decimal("21000.00")  # 3000 USD * 7.0


def test_missing_fx_rate_marks_stale(db_session: Session) -> None:
    # No USDHKD seeded → HKD holding cannot convert.
    db_session.add(FxRate(pair="USDCNY", rate=Decimal("7.0"), rate_date=_FX_DATE, source="test"))
    db_session.add(_stock("HK Co", "0700.HK", "HKD", "100", "80"))
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.total_base == Decimal("0")
    assert "0700.HK" in snap.stale_tickers


def test_empty_portfolio_has_no_concentration(db_session: Session) -> None:
    _seed_fx(db_session)
    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")
    assert snap.total_base == Decimal("0")
    assert snap.concentration.top_holding_name is None


def test_holdings_ordered_by_position_matching_holdings_edit_book_order(
    db_session: Session,
) -> None:
    """Grok review round 3 (PR #322): compute_portfolio() queried holdings
    with no ORDER BY — Postgres gives no row-order guarantee, so the
    dashboard could reshuffle rows relative to the book order the user set
    on /holdings/edit (same sort key as _sorted_holdings in
    app/routers/holdings.py: position, then name as a stable tiebreaker for
    a null/tied position)."""
    _seed_fx(db_session)
    third = _stock("Charlie Corp", "CCC", "USD", "1", "10")
    third.position = 2
    first = _stock("Alpha Inc", "AAA", "USD", "1", "10")
    first.position = 0
    second = _stock("Bravo Ltd", "BBB", "USD", "1", "10")
    second.position = 1
    # Insert out of book order — a naive unordered SELECT would likely
    # return them in this insertion order, not position order.
    db_session.add_all([third, first, second])
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert [hv.name for hv in snap.holdings] == ["Alpha Inc", "Bravo Ltd", "Charlie Corp"]


def test_user_isolation(db_session: Session) -> None:
    """compute_portfolio must not include holdings belonging to another user."""
    _seed_fx(db_session)
    other_user = uuid.UUID("00000000-0000-0000-0000-000000000002")
    db_session.add_all(
        [
            _stock("Apple", "AAPL", "USD", "10", "300"),  # belongs to _USER
            Holding(
                user_id=other_user,
                name="Other User Stock",
                pricing_mode="auto",
                ticker="MSFT",
                currency="USD",
                shares=Decimal("10"),
                market_price=Decimal("400"),
                asset_type="stock",
                asset_class="STOCK",
            ),
        ]
    )
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.total_base == Decimal("3000.00")
    assert all(hv.ticker != "MSFT" for hv in snap.holdings)


def test_stale_priced_ticker_flagged(db_session: Session) -> None:
    """A captured close older than 4 calendar days lands in stale_priced_tickers,
    not stale_tickers — it is still included in totals."""
    _seed_fx(db_session)
    holding = _stock("Apple", "AAPL", "USD", "10", None)  # no fallback market_price
    db_session.add(holding)
    db_session.flush()

    stale_date = date(2026, 1, 2)  # 10 days before as_of
    db_session.add(
        PriceSnapshot(
            ticker="AAPL",
            market="US",
            session_node="close",
            trade_date=stale_date,
            close=Decimal("300"),
        )
    )
    db_session.flush()

    as_of = date(2026, 1, 12)
    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD", as_of=as_of)

    assert snap.stale_priced_tickers == ["AAPL"]
    assert snap.stale_tickers == []
    assert snap.total_base == Decimal("3000.00")  # still included


def test_fresh_price_not_flagged(db_session: Session) -> None:
    """A captured close within 4 calendar days is not flagged as stale."""
    _seed_fx(db_session)
    holding = _stock("Apple", "AAPL", "USD", "10", None)
    db_session.add(holding)
    db_session.flush()

    trade_date = date(2026, 1, 9)  # 3 days before as_of
    db_session.add(
        PriceSnapshot(
            ticker="AAPL",
            market="US",
            session_node="close",
            trade_date=trade_date,
            close=Decimal("300"),
        )
    )
    db_session.flush()

    as_of = date(2026, 1, 12)
    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD", as_of=as_of)

    assert snap.stale_priced_tickers == []
    assert snap.stale_tickers == []
    assert snap.total_base == Decimal("3000.00")


def test_to_base_cross_currency() -> None:
    fx = {"USDHKD": Decimal("8.0"), "USDCNY": Decimal("7.0")}
    # 80 HKD → 10 USD → 70 CNY
    assert _to_base(Decimal("80"), "HKD", "CNY", fx) == Decimal("70")
    # same currency is identity
    assert _to_base(Decimal("42"), "USD", "USD", fx) == Decimal("42")
    # missing pair → None
    assert _to_base(Decimal("1"), "JPY", "USD", fx) is None


def test_currency_to_fx_pair_covers_every_valid_currency_except_usd() -> None:
    """issue #204: GBP (and 10 other VALID_CURRENCIES entries) had no FX pair
    here, so _to_base always returned None for them. Pin the full set so a
    future currency addition to VALID_CURRENCIES can't reintroduce the gap."""
    from app.schemas.holdings import VALID_CURRENCIES

    assert set(_CURRENCY_TO_FX_PAIR) == VALID_CURRENCIES - {"USD"}


def test_currency_to_fx_pair_matches_fx_fetcher_pairs_exactly() -> None:
    """Review finding, PR #253: both _CURRENCY_TO_FX_PAIR and fx_fetcher._PAIRS
    independently pin to VALID_CURRENCIES, but neither pinned to the OTHER —
    a pair name typo'd differently in the two tables (e.g. GBP -> GBPUSD here
    vs USDGBP in fx_fetcher) would pass both existing drift guards while
    still breaking every GBP conversion, since fx_rates would never contain
    the key this module looks up."""
    from app.services import fx_fetcher

    assert set(_CURRENCY_TO_FX_PAIR.values()) == set(fx_fetcher._PAIRS)


def test_gbp_holding_converts_to_base(db_session: Session) -> None:
    """issue #204: GBP was a VALID_CURRENCIES entry with no FX pair, so any
    GBP-denominated holding silently landed in stale_tickers regardless of
    whether its price was correct."""
    db_session.add(FxRate(pair="USDGBP", rate=Decimal("0.75"), rate_date=_FX_DATE, source="test"))
    db_session.add(_stock("Pershing Square Holdings", "PSH", "GBP", "10", "60"))
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.stale_tickers == []
    # 600 GBP / 0.75 = 800 USD
    assert snap.total_base == Decimal("800.00")


def test_psh_ticker_resolves_captured_price_via_lse_normalization(db_session: Session) -> None:
    """issue #204: bare 'PSH' collides with an unrelated US ETF on yfinance.
    The capture layer stores the real Pershing Square Holdings close under
    the normalized 'PSH.L' key; lookup must apply the same normalization to
    the user's stored 'PSH' ticker to find it."""
    db_session.add(FxRate(pair="USDGBP", rate=Decimal("0.75"), rate_date=_FX_DATE, source="test"))
    holding = _stock("Pershing Square Holdings", "PSH", "GBP", "10", None)
    db_session.add(holding)
    db_session.flush()

    db_session.add(
        PriceSnapshot(
            ticker="PSH.L",
            market="US",
            session_node="close",
            trade_date=date.today(),
            close=Decimal("59.00"),
        )
    )
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.stale_tickers == []
    # 590 GBP / 0.75 = 786.67 USD
    assert snap.total_base == Decimal("786.67")


def test_by_group_and_by_broker_aggregation(db_session: Session) -> None:
    """by_group keys on Holding.portfolio (None/empty -> 'Ungrouped');
    by_broker keys on Holding.broker (None/empty -> 'Other'), matching
    report_sections.py's existing broker fallback literal (issue #320,
    renamed from by_account in issue #330 since it's a custodian rollup,
    not per-account)."""
    _seed_fx(db_session)
    apple = _stock("Apple", "AAPL", "USD", "10", "300")
    apple.portfolio = "Retirement"
    apple.broker = "Fidelity"
    cash = _cash("USD Cash", "USD", "1000")  # portfolio/broker left None
    db_session.add_all([apple, cash])
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.by_group == {"Retirement": Decimal("3000.00"), "Ungrouped": Decimal("1000.00")}
    assert snap.by_broker == {"Fidelity": Decimal("3000.00"), "Other": Decimal("1000.00")}


def test_by_group_and_by_broker_exclude_unpriced_holdings(db_session: Session) -> None:
    """Same exclusion gate as by_market — a holding with no market_value_base
    never reaches the by_group/by_broker aggregation branch."""
    _seed_fx(db_session)
    db_session.add_all(
        [
            _stock("Apple", "AAPL", "USD", "10", "300"),
            _stock("PSH", "PSH.L", "GBP", None, None),  # unpriced, excluded
        ]
    )
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.by_group == {"Ungrouped": Decimal("3000.00")}
    assert snap.by_broker == {"Other": Decimal("3000.00")}


def test_by_account_aggregation_keys_on_holding_account(db_session: Session) -> None:
    """by_account (issue #330) keys on the free-text Holding.account field,
    separate from by_broker's custodian rollup (None/empty -> 'Other')."""
    _seed_fx(db_session)
    apple = _stock("Apple", "AAPL", "USD", "10", "300")
    apple.account = "Individual Brokerage"
    cash = _cash("USD Cash", "USD", "1000")  # account left None
    db_session.add_all([apple, cash])
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.by_account == {
        "Individual Brokerage": Decimal("3000.00"),
        "Other": Decimal("1000.00"),
    }


def test_by_account_excludes_unpriced_holdings(db_session: Session) -> None:
    """Same exclusion gate as by_broker/by_market — an unpriced holding never
    reaches the by_account aggregation branch."""
    _seed_fx(db_session)
    db_session.add_all(
        [
            _stock("Apple", "AAPL", "USD", "10", "300"),
            _stock("PSH", "PSH.L", "GBP", None, None),  # unpriced, excluded
        ]
    )
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.by_account == {"Other": Decimal("3000.00")}


def test_pnl_computed_for_auto_priced_holding_with_cost_basis(db_session: Session) -> None:
    _seed_fx(db_session)
    apple = _stock("Apple", "AAPL", "USD", "10", "300")
    apple.avg_cost = Decimal("250")
    db_session.add(apple)
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    hv = snap.holdings[0]
    assert hv.cost_basis_base == Decimal("2500.00")
    assert hv.unrealized_pnl_base == Decimal("500.00")
    assert hv.unrealized_pnl_pct == Decimal("0.2000")  # 500 / 2500


def test_pnl_none_for_cash_holding(db_session: Session) -> None:
    """Cash/wmf holdings have no cost-basis concept — all three fields are
    None (not zero), so the frontend renders '—' rather than a fake $0 P&L."""
    _seed_fx(db_session)
    db_session.add(_cash("USD Cash", "USD", "1000"))
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    hv = snap.holdings[0]
    assert hv.cost_basis_base is None
    assert hv.unrealized_pnl_base is None
    assert hv.unrealized_pnl_pct is None


def test_pnl_none_when_avg_cost_missing(db_session: Session) -> None:
    """avg_cost is optional on auto-priced holdings (e.g. imported without a
    cost basis) — P&L stays None rather than assuming a zero cost basis."""
    _seed_fx(db_session)
    db_session.add(_stock("Apple", "AAPL", "USD", "10", "300"))  # avg_cost unset
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    hv = snap.holdings[0]
    assert hv.unrealized_pnl_base is None
    assert hv.cost_basis_base is None


def test_pnl_none_for_capture_unsupported_holding(db_session: Session) -> None:
    """A holding with capture_supported=False has market_value_base=None
    regardless of avg_cost — P&L can't be computed against no valuation."""
    _seed_fx(db_session)
    h = _stock("Unresolvable", None, "GBP", "10", None)
    h.market = "Other"
    h.capture_supported = False
    h.avg_cost = Decimal("5")
    db_session.add(h)
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    hv = snap.holdings[0]
    assert hv.market_value_base is None
    assert hv.unrealized_pnl_base is None


def test_pnl_totals_exclude_cash_and_sum_priced_holdings_only(db_session: Session) -> None:
    """Snapshot-level P&L totals sum only holdings with a computed cost basis
    (issue #320 decision 2) — cash/wmf never contributes to the numerator or
    denominator, so 'total unrealized return %' doesn't silently include
    assets that have no cost-basis concept."""
    _seed_fx(db_session)
    apple = _stock("Apple", "AAPL", "USD", "10", "300")
    apple.avg_cost = Decimal("250")  # cost 2500, value 3000, pnl +500
    msft = _stock("Microsoft", "MSFT", "USD", "5", "100")
    msft.avg_cost = Decimal("80")  # cost 400, value 500, pnl +100
    db_session.add_all([apple, msft, _cash("USD Cash", "USD", "5000")])
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.total_cost_basis_base == Decimal("2900.00")  # 2500 + 400
    assert snap.total_unrealized_pnl_base == Decimal("600.00")  # 500 + 100
    assert snap.total_unrealized_pnl_pct == Decimal("0.2069")  # 600 / 2900


def test_pnl_totals_zero_when_no_priced_holdings(db_session: Session) -> None:
    _seed_fx(db_session)
    db_session.add(_cash("USD Cash", "USD", "1000"))
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.total_cost_basis_base == Decimal("0")
    assert snap.total_unrealized_pnl_base == Decimal("0")
    assert snap.total_unrealized_pnl_pct is None  # never divide by zero


def test_price_as_of_date_is_max_captured_trade_date_actually_used(
    db_session: Session,
) -> None:
    """price_as_of_date is the max trade_date among captured closes actually
    matched to one of this user's holdings — not just any row in
    price_snapshots (issue #320 decision 5)."""
    _seed_fx(db_session)
    aapl = _stock("Apple", "AAPL", "USD", "10", None)
    msft = _stock("Microsoft", "MSFT", "USD", "5", None)
    db_session.add_all([aapl, msft])
    db_session.flush()
    db_session.add_all(
        [
            PriceSnapshot(
                ticker="AAPL",
                market="US",
                session_node="close",
                trade_date=date(2026, 1, 9),
                close=Decimal("300"),
            ),
            PriceSnapshot(
                ticker="MSFT",
                market="US",
                session_node="close",
                trade_date=date(2026, 1, 8),
                close=Decimal("400"),
            ),
        ]
    )
    db_session.flush()

    snap = compute_portfolio(
        db_session, user_id=_USER, base_currency="USD", as_of=date(2026, 1, 12)
    )

    assert snap.price_as_of_date == date(2026, 1, 9)  # max of the two, not the DB max


def test_price_as_of_date_ignores_a_captured_close_that_never_priced_a_row(
    db_session: Session,
) -> None:
    """Grok review round 2 (PR #322): a snapshot match must actually produce
    a market_value_base before its trade_date counts — matching the table
    isn't enough. AAPL has shares and prices on Jan 9; MSFT's snapshot on
    Jan 10 is newer but MSFT has no shares, so nothing on the page reflects
    a Jan 10 price — the banner must not claim it does."""
    _seed_fx(db_session)
    aapl = _stock("Apple", "AAPL", "USD", "10", None)
    msft = _stock("Microsoft", "MSFT", "USD", None, None)  # no shares -> unpriceable
    db_session.add_all([aapl, msft])
    db_session.flush()
    db_session.add_all(
        [
            PriceSnapshot(
                ticker="AAPL",
                market="US",
                session_node="close",
                trade_date=date(2026, 1, 9),
                close=Decimal("300"),
            ),
            PriceSnapshot(
                ticker="MSFT",
                market="US",
                session_node="close",
                trade_date=date(2026, 1, 10),
                close=Decimal("400"),
            ),
        ]
    )
    db_session.flush()

    snap = compute_portfolio(
        db_session, user_id=_USER, base_currency="USD", as_of=date(2026, 1, 12)
    )

    assert snap.price_as_of_date == date(2026, 1, 9)  # not Jan 10 — MSFT never priced


def test_price_as_of_date_none_when_nothing_captured(db_session: Session) -> None:
    _seed_fx(db_session)
    db_session.add(_cash("USD Cash", "USD", "1000"))
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.price_as_of_date is None


def test_holding_value_exposes_pricing_mode_and_capture_supported(db_session: Session) -> None:
    """Issue #320 decision 3: the frontend partitions on pricing_mode=='auto'
    and not capture_supported — both must be readable off HoldingValue."""
    _seed_fx(db_session)
    unsupported = _stock("Unresolvable", None, "GBP", "10", None)
    unsupported.market = "Other"
    unsupported.capture_supported = False
    db_session.add_all([_cash("USD Cash", "USD", "1000"), unsupported])
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    by_name = {hv.name: hv for hv in snap.holdings}
    assert by_name["USD Cash"].pricing_mode == "manual"
    assert by_name["Unresolvable"].pricing_mode == "auto"
    assert by_name["Unresolvable"].capture_supported is False


def test_holding_value_exposes_notes(db_session: Session) -> None:
    """Grok review round 2 (PR #322): issue #320 decision 3 / comment 2 both
    list `notes` as one of the user-entered fields the no-quote block must
    show; the frozen HoldingValueOut contract table omitted it (a doc gap,
    not a deliberate exclusion) — added here to close it."""
    _seed_fx(db_session)
    unsupported = _stock("Unresolvable", None, "GBP", "10", None)
    unsupported.market = "Other"
    unsupported.capture_supported = False
    unsupported.notes = "Private placement, no public ticker"
    db_session.add(unsupported)
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.holdings[0].notes == "Private placement, no public ticker"


def test_unpriced_holding_kept_in_list_but_excluded_from_aggregates(
    db_session: Session,
) -> None:
    """Issue #295: a holding with no captured price must keep its row in the
    snapshot (market_value / market_value_base None — the §1 row renders a
    placeholder instead of vanishing, which reads as data loss) while staying
    out of every aggregate and out of concentration math."""
    _seed_fx(db_session)
    db_session.add_all(
        [
            _stock("Apple", "AAPL", "USD", "10", "300", sector="Technology"),
            _stock("PSH", "PSH.L", "GBP", None, None),  # no price captured
        ]
    )
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    by_name = {hv.name: hv for hv in snap.holdings}
    assert "PSH" in by_name  # row is kept, not dropped
    psh = by_name["PSH"]
    assert psh.market_value is None
    assert psh.market_value_base is None
    assert "PSH.L" in snap.stale_tickers
    # aggregates reflect Apple only
    assert snap.total_base == Decimal("3000.00")
    assert snap.by_currency == {"USD": Decimal("3000.00")}
    assert snap.by_asset_type == {"stock": Decimal("3000.00")}
    assert snap.by_market == {"US": Decimal("3000.00")}
    assert snap.by_asset_class == {"STOCK": Decimal("3000.00")}
    assert snap.concentration.top_holding_name == "Apple"
    assert snap.concentration.top3_ratio == Decimal("1.0000")
