from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.ticker_intel import TickerIntel
from app.services import cross_name_intel
from app.services.instrument_universe import UniverseEntry
from app.services.intel_selection import WorkUnit
from app.services.intel_shared_analysis import l1_candidates, run_post_close_analysis
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_intel_deepen_rules import NOW
from app.tests.test_intel_paid import slot


def test_12_l3_strongest_25(db_session: Session) -> None:
    for i in range(30):
        db_session.add(
            TickerIntel(
                identifier=f"AAA{i:02}",
                trade_date=NOW.date(),
                prompt_version="l1-v4",
                model="fixture",
                analysis="An agreement was announced.",
                facts={"day_pct": (-1 if i % 2 else 1) * i / 100},
                attempt_count=1,
            )
        )
    db_session.flush()
    with patch.object(cross_name_intel, "_generate", return_value=([], 1)) as generate:
        cross_name_intel.get_day_synthesis(db_session, NOW.date())
    assert set(generate.call_args.args[3].briefings) == {f"AAA{i:02}" for i in range(5, 30)}


def test_17_large_weight_selection_and_global_requests(db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)
    for ticker, value in [("FFF", 30), ("AAA", 70)]:
        db_session.add(
            PortfolioValueSnapshot(
                user_id=TEST_USER_ID,
                snapshot_date=NOW.date() - timedelta(days=1),
                ticker=ticker,
                market="US",
                currency="USD",
                base_currency="USD",
                market_value_base=Decimal(value),
            )
        )
    db_session.flush()
    universe = [UniverseEntry(t, t, "US") for t in ["FFF", "AAA"]]
    assert "FFF" in l1_candidates(db_session, universe, [])
    run = slot(db_session)
    with (
        patch("app.services.intel_shared_analysis.get_l1_intel_batch", return_value={}) as l1,
        patch("app.services.intel_shared_analysis.get_l2_intel_batch", return_value={}),
        patch("app.services.intel_shared_analysis.get_day_synthesis", return_value=[]),
    ):
        run_post_close_analysis(db_session, run, [WorkUnit("mover", "AAA")], universe)
    assert "FFF" in l1.call_args.args[1]
    assert "market_value" not in str(l1.call_args.args[3]) and "0.3" not in str(
        l1.call_args.args[3]
    )


def test_shared_failures_are_independent(db_session: Session) -> None:
    run = slot(db_session)
    with (
        patch(
            "app.services.intel_shared_analysis.get_l1_intel_batch",
            side_effect=ValueError("failure"),
        ),
        patch("app.services.intel_shared_analysis.get_l2_intel_batch", return_value={}) as l2,
        patch("app.services.intel_shared_analysis.get_day_synthesis", return_value=[]) as l3,
    ):
        result = run_post_close_analysis(db_session, run, [], [])
    assert l2.called and l3.called and result["errors"] == ["L1: ValueError"]


def test_17_outbound_l1_prompt_contains_only_global_facts(db_session: Session) -> None:
    from app.models.holding import Holding
    from app.models.price_snapshot import PriceSnapshot

    for days, close in [(1, 100), (0, 101)]:
        db_session.add(
            PriceSnapshot(
                ticker="FFF",
                market="US",
                session_node="close",
                trade_date=NOW.date() - timedelta(days=days),
                close=Decimal(close),
                captured_at=NOW - timedelta(days=days),
            )
        )
    seed_user(db_session, TEST_USER_ID)
    db_session.add(
        Holding(
            user_id=TEST_USER_ID,
            name="FFF",
            ticker="FFF",
            pricing_mode="auto",
            currency="USD",
            asset_type="stock",
            market="US",
        )
    )
    for ticker, value in [
        ("FFF", 30),
        ("AAA", 20),
        ("BBB", 15),
        ("CCC", 15),
        ("DDD", 10),
        ("EEE", 10),
    ]:
        db_session.add(
            PortfolioValueSnapshot(
                user_id=TEST_USER_ID,
                snapshot_date=NOW.date(),
                ticker=ticker,
                market="US",
                currency="USD",
                base_currency="USD",
                market_value_base=Decimal(value),
            )
        )
    db_session.flush()
    with (
        patch("app.services.ticker_intel._openrouter_client"),
        patch(
            "app.services.ticker_intel._call_llm", return_value="An agreement was announced."
        ) as outbound,
        patch("app.services.intel_shared_analysis.get_l2_intel_batch", return_value={}),
        patch("app.services.intel_shared_analysis.get_day_synthesis", return_value=[]),
    ):
        run_post_close_analysis(
            db_session, slot(db_session), [], [UniverseEntry("FFF", "FFF", "US")]
        )
    assert outbound.call_count == 1
    prompt = outbound.call_args.args[3]
    assert "FFF" in prompt
    assert "30%" not in prompt and "0.3" not in prompt
    assert str(TEST_USER_ID) not in prompt and "market_value_base" not in prompt
    assert db_session.query(TickerIntel).filter_by(identifier="FFF").one().analysis
