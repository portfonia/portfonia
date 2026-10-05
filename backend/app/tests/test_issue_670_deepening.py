"""Issue #670: Parallel extract payload, weekend price selection, body markers."""

from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import httpx
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.price_snapshot import PriceSnapshot
from app.services.instrument_universe import UniverseEntry
from app.services.intel_body import body_verdict
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_selection import select_units
from app.services.intel_signals import Signal, compute_signals
from app.services.paid_search import ParallelClient
from app.tests.test_intel_deepen_rules import NOW

SATURDAY = NOW + timedelta(days=1)

# Shapes of two bodies accepted by the 2026-10-05 pre_open slot (shortened).
LOGIN_TEASER = (
    "# Rocket reaches orbit, but engine issues raise stakes for the Moon plan\n"
    "The rocket reached Earth orbit for the first time on its 14th test flight and "
    "deployed 26 satellites, marking a major milestone for the heavy-lift rocket...\n"
    "**Keep me signed in**\n"
    "Some subscribers prefer to save their log-in information so they do not have to "
    "enter their User ID and Password each time they visit the site. To activate this "
    "function, check the 'Keep me signed in' box in the log-in section."
)
AGGREGATOR = (
    "Read NextBitcoin Is Back Near September Highs - And Shorts Are Stacked Just Above "
    "Market Author·8m ago Image 131: Replica coins are seen in this photo illustration.\n"
    "## Battery Swaps Hit Holiday High\n"
    "The carmaker completed 183,469 battery swaps on Oct. 1, its highest single-day "
    "total. The holiday travel rush pushed daily swaps about 4.3% above the record.\n"
    "Advertisement|Remove ads. [...] Image 42: News Image Bitcoin Is Back Near September "
    "Highs Image 43: Author image Basu·8m ago Image 44: News Image Stock Rises Premarket: "
    "Company Wins New Contract To Power Next Station Module Image 45: Author image 51m ago "
    "Image 46: News Image Futures Steady Ahead Of Fed Minutes This Week: Stocks In Focus "
    "Image 47: Author image 1h ago Image 48: News Image Stocks Rebound Premarket [...] "
    "When you visit our website, we store cookies on your browser to collect information. "
    "The information collected might relate to you, your preferences or your device, and "
    "is mostly used to make the site work as you expect it to and to provide a more "
    "personalized web experience. However, you can choose not to allow certain types of "
    "cookies, which may impact your experience of the site and the services we offer."
)


def test_670_01_parallel_extract_nests_full_content() -> None:
    settings = get_settings().model_copy(update={"PARALLEL_API_KEY": SecretStr("fixture")})
    sent: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        sent.append(kwargs["json"])
        return httpx.Response(
            200,
            json={"results": [{"url": "https://fixture.example/a", "full_content": "Body."}]},
        )

    with patch("app.services.paid_search.post", side_effect=post):
        result = ParallelClient(settings, 3).extract(["https://fixture.example/a"], "AAA")
    assert sent[0]["advanced_settings"] == {"full_content": True}
    assert "full_content" not in sent[0]
    assert result.bodies == {"https://fixture.example/a": "Body."}


def test_670_02_weekend_selects_movers_and_near() -> None:
    cfg = load_intel_deepen_config()
    signals = {
        "MOV": Signal("MOV", mover=True, strength=0.07, reason="d1 +7.0%", filings=1),
        "NEAR": Signal("NEAR", near=True, strength=1.5, reason="near_d5 +13.5%"),
    }
    units = select_units(signals, {}, cfg, weekend=True)
    assert [(u.kind, u.identifier, u.reason) for u in units] == [
        ("mover", "MOV", "d1 +7.0%"),
        ("quiet", "NEAR", "near_d5 +13.5%"),
    ]


def test_670_03_weekend_signals_read_closes(db_session: Session) -> None:
    for i, value in enumerate([100, 100, 100, 100, 100, 106]):
        db_session.add(
            PriceSnapshot(
                ticker="AAA",
                market="US",
                session_node="close",
                trade_date=NOW.date() - timedelta(days=5 - i),
                close=Decimal(value),
            )
        )
    db_session.flush()
    signals = compute_signals(
        db_session,
        [UniverseEntry("AAA", "AAA", "US")],
        SATURDAY.date(),
        SATURDAY - timedelta(hours=12),
        load_intel_deepen_config(),
        slot="pre_open",
        now=SATURDAY,
        weekend=True,
    )
    assert signals["AAA"].mover
    assert signals["AAA"].reason == "d1 +6.0%"


def test_670_05_login_teaser_and_aggregator_bodies_rejected() -> None:
    cfg = load_intel_deepen_config()
    assert body_verdict(LOGIN_TEASER, cfg) == (False, "paywall")
    assert body_verdict(AGGREGATOR, cfg) == (False, "boilerplate")
