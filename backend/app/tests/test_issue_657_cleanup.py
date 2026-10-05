"""Issue #657 acceptance; every external provider is mocked."""

import hashlib
from datetime import datetime, timedelta
from typing import cast
from unittest.mock import patch

import httpx
import pandas as pd
import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.timezones import ET
from app.models.intel import InstrumentProfile, IntelSlotRun, NewsInstrument
from app.models.news import News
from app.services import headline_cleaning as hc
from app.services import instrument_news_capture as capture
from app.services import intel_deepen as deepen
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_universe import UniverseEntry
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_digest import SOURCE_LINES, problem_lines
from app.services.intel_leads import Lead, select_headlines, select_leads
from app.services.intel_selection import WorkUnit
from app.services.intel_signals import compute_signals
from app.tests.test_intel_paid import slot
from app.tests.test_issue_630_classifier import response
from app.tests.test_issue_635_deepening import worker as shared_worker  # noqa: F401
from app.tests.test_issue_635_report import collection, report

NOW = datetime(2026, 10, 4, 16, 15, tzinfo=ET)


@pytest.fixture
def worker(request: pytest.FixtureRequest) -> deepen.DeepenRun:
    return cast(deepen.DeepenRun, request.getfixturevalue("shared_worker"))


def stored(
    session: Session, title: str, published: datetime, linked: datetime, *, kind: str = "article"
) -> News:
    row = News(
        url_hash=hashlib.md5(title.encode()).hexdigest()[:16],
        record={"title": title},
        kind=kind,
        published_at=published,
        fetched_at=linked,
    )
    session.add(row)
    session.flush()
    session.add(NewsInstrument(news_id=row.id, identifier="AAA", created_at=linked))
    session.flush()
    return row


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        ("US", ["finnhub", "google_news", "yahoo", "sec"]),
        ("HK", ["google_news", "google_news", "yahoo", "eastmoney"]),
        ("A-Share", ["google_news", "eastmoney"]),
    ],
)
def test_657_a1_google_sources_opt_in(market: str, expected: list[str]) -> None:
    assert Settings.model_fields["INTEL_GOOGLE_NEWS_ENABLED"].default is False
    entry = UniverseEntry("AAA", "AAA", market)
    profile = InstrumentProfile(
        identifier="AAA", market=market, name_en="Alpha", name_zh="Alpha Chinese"
    )
    for enabled in (False, True):
        settings = get_settings().model_copy(update={"INTEL_GOOGLE_NEWS_ENABLED": enabled})
        with patch.object(capture, "get_settings", return_value=settings):
            names = [
                name
                for name, _ in capture.sources_for(entry, profile, NOW - timedelta(days=2), NOW)
            ]
        assert names == (expected if enabled else [x for x in expected if x != "google_news"])


@pytest.mark.parametrize("market", ["US", "HK", "A-Share"])
def test_657_a2_disabled_no_missing_name_count(db_session: Session, market: str) -> None:
    settings = get_settings().model_copy(update={"INTEL_GOOGLE_NEWS_ENABLED": False})
    with (
        patch.object(capture, "get_settings", return_value=settings),
        patch.object(capture, "sources_for", return_value=[]),
    ):
        result = capture.collect_instrument_news(
            db_session, UniverseEntry("AAA", "AAA", market), NOW, hc.load_cleaning_config()
        )
    assert result.stats.get("google_news", {}).get("skipped_no_name", 0) == 0
    assert result.stats.get("yahoo", {}).get("skipped_no_name", 0) == int(market == "HK")


def test_657_a3_report_omits_idle_google(db_session: Session) -> None:
    run = slot(db_session)
    collection(db_session, run, {})
    _, body, _ = report(db_session, run)
    assert "Google News ......." not in body
    for source in ("finnhub", "yahoo", "sec", "eastmoney"):
        assert f"  {SOURCE_LINES[source]} 0 (0 requests)" in body
    collection(db_session, run, {"google_news": {"calls": 1, "items": 2}})
    assert "Google News ....... 2 (1 requests)" in report(db_session, run)[1]


@pytest.mark.parametrize(("seconds", "count"), [(-1, 0), (0, 1), (5, 1)])
def test_657_a4_signal_current_run(db_session: Session, seconds: int, count: int) -> None:
    stored(db_session, "AAA filing", NOW, NOW + timedelta(seconds=seconds), kind="filing")
    signal = compute_signals(
        db_session,
        [UniverseEntry("AAA", "AAA", "US")],
        NOW.date(),
        NOW - timedelta(hours=8),
        load_intel_deepen_config(),
        slot="post_close",
        now=NOW,
        weekend=True,
    )["AAA"]
    assert signal.filings == count
    assert signal.fresh == count


@pytest.mark.parametrize("url_kind", ["direct", "google_news"])
@pytest.mark.parametrize("reason", ["new_filing", "news_spike"])
def test_657_a5_leads_current_run(db_session: Session, reason: str, url_kind: str) -> None:
    items = []
    for seconds in (-1, 0, 5):
        title = f"AAA agreement {seconds}"
        row = stored(db_session, title, NOW, NOW + timedelta(seconds=seconds))
        items.append(
            CollectedItem(
                title,
                NOW,
                f"https://fixture{seconds}.example/article",
                news_id=row.id,
                url_kind=url_kind,
            )
        )
    unit = WorkUnit("quiet", "AAA", reason=reason)
    cfg = load_intel_deepen_config()
    selected = (
        select_leads(db_session, unit, items, ["AAA"], cfg, NOW)
        if url_kind == "direct"
        else select_headlines(db_session, unit, items, ["AAA"], cfg, now=NOW)
    )
    assert [x.title for x in selected] == [x.title for x in items[1:]]


@pytest.mark.parametrize(
    ("status", "category", "wording"),
    [
        (422, "error", "request failed (HTTP 422)"),
        (401, "invalid_key", "API key is invalid"),
        (429, "quota_or_rate", "provider quota or request limit reached"),
    ],
)
def test_657_a6_provider_response_saved(
    worker: deepen.DeepenRun, db_session: Session, status: int, category: str, wording: str
) -> None:
    worker.settings = worker.settings.model_copy(
        update={"PARALLEL_API_KEY": SecretStr("fixture-only")}
    )
    worker.usage.configured["parallel"] = True
    body = "  provider\n rejected\t " + "long response " * 30
    excerpt = " ".join(body.split())[:200]
    with patch("app.services.paid_search.post", return_value=httpx.Response(status, text=body)):
        worker._call("parallel", "extract", "AAA", leads=[Lead("https://fixture.example/a", "AAA")])
    expected = f"parallel extract: {category} HTTP {status} {excerpt}"
    assert worker.errors == [expected]
    assert problem_lines(worker.errors) == ["Problems:", f"  Parallel: {wording} (1 times)"]
    run = db_session.get(IntelSlotRun, worker.run_id)
    assert run is not None
    run.details = {"deepening": worker.details(), "deepening_errors": list(worker.errors)}
    db_session.flush()
    db_session.expire(run)
    assert run.details["deepening_errors"] == [expected]
    deepening = run.details["deepening"]
    assert isinstance(deepening, dict)
    assert deepening["errors"] == [expected]


@pytest.mark.parametrize(
    ("symbol", "title", "publish_day", "earnings_day", "expected"),
    [
        (
            "ASML",
            "ASML Holding stock rises 2.77 percent ahead of Q3 results",
            2,
            "2026-10-14",
            None,
        ),
        (
            "TSLA",
            "Tesla's Delivery Surprise What to Watch Before October 21 Earnings",
            2,
            "2026-10-21",
            None,
        ),
        ("GOOGL", "Microsoft (MSFT) Q4 FY2026 Preview July 30 Test", 3, "2026-10-28", "stale_rule"),
        ("SPCX", "Wall Street Awaits AMD, SpaceX Earnings", 3, "2026-11-03", "stale_rule"),
        ("AAA", "AAA ahead of results", 3, "2026-07-23", "stale_lookup_failed"),
        (
            "INTC",
            "Why Is Intel (INTC) Stock Volatile After Earnings?",
            3,
            "2026-07-23",
            "stale_rule",
        ),
    ],
)
@pytest.mark.parametrize("recap", [False, True])
def test_657_a7_preview_dates(
    symbol: str, title: str, publish_day: int, earnings_day: str, expected: str | None, recap: bool
) -> None:
    frame = pd.DataFrame(
        index=pd.DatetimeIndex([datetime.fromisoformat(earnings_day).replace(tzinfo=ET)])
    )
    item = CollectedItem(title, NOW.replace(day=publish_day), "https://fixture.example/a")
    with patch("yfinance.Ticker.get_earnings_dates", return_value=frame):
        assert (
            hc.EarningsCache().stale_reason(item, symbol, hc.load_cleaning_config(), recap=recap)
            == expected
        )


@pytest.mark.parametrize("future", [False, True])
def test_657_a8_preview_checked_once(db_session: Session, future: bool) -> None:
    item = CollectedItem("AAA ahead of results", NOW, "https://fixture.example/a")
    frame = pd.DataFrame(index=pd.DatetimeIndex([NOW + timedelta(days=10)] if future else []))
    cache = hc.EarningsCache()
    with (
        patch.object(capture, "sources_for", return_value=[("yahoo", lambda: [item])]),
        patch.object(capture, "classify_headlines", hc.classify_headlines),
        patch.object(
            httpx, "post", return_value=response([{"id": 0, "label": "keep", "recap": True}])
        ),
        patch("yfinance.Ticker.get_earnings_dates", return_value=frame) as lookup,
        patch.object(cache, "stale_reason", wraps=cache.stale_reason) as check,
    ):
        result = capture.collect_instrument_news(
            db_session,
            UniverseEntry("AAA", "AAA", "US"),
            NOW,
            hc.load_cleaning_config(),
            earnings_cache=cache,
        )
    assert lookup.call_count == 1
    assert check.call_count == 1
    assert result.cleaning.get("stale_lookup_failed", 0) == int(not future)
    assert result.leads == [item]


def test_657_a9_duplicate_window_and_existing(db_session: Session) -> None:
    title = "AAA announces a new factory in Ohio"
    stored(db_session, title, NOW.replace(day=2, hour=16, minute=7), NOW.replace(hour=7, minute=30))
    old_title = "AAA signs supply agreement in Europe"
    stored(db_session, old_title, NOW - timedelta(hours=60), NOW - timedelta(days=1))
    candidates = [
        CollectedItem(title, NOW.replace(day=2, hour=18, minute=3), "https://fixture.example/a"),
        CollectedItem("AAA appoints chief financial officer", NOW, "https://fixture.example/b"),
    ]
    with (
        patch.object(capture, "sources_for", return_value=[("yahoo", lambda: candidates)]),
        patch.object(
            capture, "classify_headlines", return_value=({0: "keep"}, 0, None)
        ) as classifier,
    ):
        result = capture.collect_instrument_news(
            db_session, UniverseEntry("AAA", "AAA", "US"), NOW, hc.load_cleaning_config()
        )
    assert result.cleaning.get("duplicate_earlier", 0) == 1
    assert [x.title for x in result.leads] == [candidates[1].title]
    assert classifier.call_args.kwargs["recent_titles"] is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Intel Corp (xINTC) Stock Price Today", "low_value_rule"),
        ("Advanced Micro Devices (AMD) Stock Price, Quote & Analysis", "low_value_rule"),
        ("Applied Optoelectronics, Inc. (AAOI) latest stock news and headlines", "low_value_rule"),
        ("$Micron Technology (MU.US)$$SK hynix ...", "low_value_rule"),
        ("GOOGL CLASS ACTION NOTICE: ...", "low_value_rule"),
        ("GOOGL DEADLINE ALERT: ...", "low_value_rule"),
        ("Investor Alert: Robbins LLP ...", "low_value_rule"),
        ("Form 4 Alphabet Inc Class A For: 2 October By Investing.com", "low_value_rule"),
        ("Alphabet faces class action over ad tech", None),
        ("Nvidia stock jumps after buyback", None),
    ],
)
def test_657_a10_low_value_titles(title: str, expected: str | None) -> None:
    item = CollectedItem(title, NOW, "https://fixture.example/a")
    assert (
        hc.block_reason(
            item,
            ["Intel", "AMD", "AAOI", "Micron", "GOOGL", "Robbins", "Alphabet", "Nvidia"],
            [],
            hc.load_cleaning_config(),
        )
        == expected
    )
