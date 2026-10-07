"""Issue #670: Parallel extract payload, weekend price selection, body markers."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import httpx
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.price_snapshot import PriceSnapshot
from app.services.instrument_universe import UniverseEntry
from app.services.intel_body import body_verdict, clean_body
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_selection import select_units
from app.services.intel_signals import Signal, compute_signals
from app.services.paid_search import ParallelClient
from app.tests.test_intel_deepen_rules import NOW, SESSION_DATES

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
                trade_date=SESSION_DATES[5 - i],
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


def test_670_06_ordinary_articles_with_marker_like_text_accepted() -> None:
    cfg = load_intel_deepen_config()
    p1 = (
        "Shares of the chipmaker rose after the company said quarterly revenue would beat "
        "its own guidance, citing demand from data center customers."
    )
    p2 = (
        "Portfolio managers will read next week's inflation print closely, since a hotter "
        "number could change the expected path of interest rates this year."
    )
    p3 = (
        "The company also said it expects margins to improve as new capacity comes online "
        "during the second half of the year."
    )
    cookie = (
        "When you visit our website, we store cookies on your browser to collect "
        "information. The information collected might relate to you, your preferences or "
        "your device, and is mostly used to make the site work as you expect it to and to "
        "provide a more personalized web experience. However, you can choose not to allow "
        "certain types of cookies."
    )
    for text in ("\n".join([p1, p2, p3]), "\n".join([p1, p3, cookie])):
        assert body_verdict(clean_body("https://fixture.example/a", text, cfg), cfg) == (
            True,
            None,
        )


def test_670_07_d1_window_reaches_last_close_across_weekend() -> None:
    cfg = load_intel_deepen_config()
    friday = NOW.date()
    closes = [(SESSION_DATES[i], 106.0 if i == 0 else 100.0) for i in range(6)]
    previous = NOW - timedelta(hours=12)
    sunday, monday = friday + timedelta(days=2), friday + timedelta(days=3)
    for run_date, slot in ((sunday, "post_close"), (monday, "pre_open")):
        signal = Signal.from_closes(
            "AAA",
            closes,
            cfg,
            run_date,
            slot,
            previous,
            market="US",
            now=datetime.combine(run_date, NOW.timetz()).replace(
                hour=8 if slot == "pre_open" else 17
            ),
        )
        assert signal.reason == "d1 +6.0%"
        assert signal.window_start == friday
    weekday = Signal.from_closes(
        "AAA", closes, cfg, friday, "post_close", previous, market="US", now=NOW
    )
    assert weekday.window_start == friday - timedelta(days=1)


def test_670_08_weekend_priority_respects_weekend_quiet_cap(db_session: Session) -> None:
    from app.services.news_capture import PoolCaptureResult
    from app.tasks import intel_tasks as task

    signals = {"MOV": Signal("MOV", mover=True, strength=0.07, reason="d1 +7.0%")}
    signals.update(
        {
            f"N{i}": Signal(f"N{i}", near=True, strength=2 - i / 10, reason="near_d5 +10.0%")
            for i in range(8)
        }
    )
    seen: list[list[str]] = []

    def collect(session: Session, run: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(list(kwargs["priority"]))
        collection = kwargs["collection_run"]
        collection.status = "ok"
        return collection

    with (
        patch.object(task, "SessionLocal", return_value=db_session),
        patch.object(task, "now_et", return_value=SATURDAY),
        patch.object(task, "today_et", return_value=SATURDAY.date()),
        patch.object(task, "intel_universe", return_value=[]),
        patch.object(task, "resolve_profiles", return_value=[]),
        patch.object(task, "compute_signals", return_value=signals),
        patch.object(task, "capture_news", return_value=PoolCaptureResult(0, [])),
        patch.object(task, "collect_slot_news", side_effect=collect),
        patch.object(task, "DeepenRun") as worker,
        patch.object(task, "send_ops_alert", return_value=True),
    ):
        worker.return_value.configure_mock(theme_counts={}, theme_counts_development={}, errors=[])
        worker.return_value.details.return_value = {}
        task.intel_slot_task("post_close")
    assert seen == [["MOV", "N0", "N1", "N2", "N3", "N4"]]


def test_670_09_batch_report_layout(db_session: Session) -> None:
    from app.services import intel_digest
    from app.tests.test_intel_paid import slot
    from app.tests.test_issue_635_report import collection

    run = slot(db_session)
    collection(
        db_session,
        run,
        {
            "cleaning": {"kept": 2, "unrelated_rule": 2, "duplicate_earlier": 1},
            "cleaning_samples": {"unrelated_rule": ["Title one", "Title two"]},
        },
    )
    mover = {"kind": "mover", "identifier": "AAOI", "theme": ""}
    run.details = {
        "deepening": {
            "selections": [
                {"kind": "mover", "identifier": "COHR", "reason": "d3 +15.3%"},
                {**mover, "reason": "d1 +7.7%"},
                {"kind": "quiet", "identifier": "LRCX", "reason": "near_d5 +10.2%"},
            ],
            "outcomes": [
                {"kind": "mover", "identifier": "COHR", "provider": None, "note": "no_news"},
                {**mover, "provider": "tavily", "via": "direct", "accepted": 1},
                {**mover, "provider": "parallel", "via": "direct", "accepted": 0},
            ],
            "metrics": {"tavily": {"accepted": 1, "cost_usd": 0.008}},
            "usage": {"tavily": {"run": 1, "month": 1, "month_limit": 1000}},
            "errors": ["parallel extract: error HTTP 422 fixture"],
        }
    }
    lines = intel_digest.build_batch_report(db_session, run)[1].splitlines()
    index = lines.index("  Do not name the company ................ 2")
    assert lines[index + 1] == '      e.g. "Title one"; "Title two"'
    assert lines[index + 2] == "  Already collected in an earlier batch .... 1"
    start = lines.index("Picked:")
    assert lines[start + 1 : start + 6] == [
        "  COHR (price move (d3 +15.3%)): no news to follow up, no paid call",
        "  AAOI (price move (d1 +7.7%)): Tavily  1 articles kept (direct links)",
        "  LRCX (close to the multi-day move threshold)",
        "",
        "Articles kept: 1. Rejected: 0. Failed: 0.",
    ]
    problems = max(i for i, line in enumerate(lines) if line == "Problems:")
    assert lines[problems - 1] == ""
