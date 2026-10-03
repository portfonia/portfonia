"""Issue #620 provider mappings and outbound public-data boundary."""

import time
from datetime import timedelta
from itertools import pairwise
from unittest.mock import patch

import httpx
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services import instrument_news_sources as src
from app.services.news_fetcher import url_hash
from app.tests.test_intel_records import NOW


def response(data: object) -> httpx.Response:
    return httpx.Response(200, json=data, request=httpx.Request("GET", "https://fixture.example"))


def test_acceptance_04_finnhub_mapping() -> None:
    rows = [
        {
            "datetime": int(NOW.timestamp()),
            "headline": "Nvidia earnings",
            "url": url,
            "summary": "Board approves",
            "source": "FixturePublisher",
        }
        for url in ["https://finnhub.io/x", "https://fixture.example/x"]
    ]
    rows.append(
        {
            "datetime": int((NOW - timedelta(days=3)).timestamp()),
            "headline": "Old",
            "url": "https://fixture.example/old",
        }
    )
    with patch.object(src, "request", return_value=response(rows)):
        result = src.fetch_finnhub("NVDA", NOW - timedelta(hours=48), NOW)
    assert [x.url_kind for x in result] == ["finnhub_redirect", "direct"]
    assert [x.title for x in result] == ["Nvidia earnings"] * 2


def test_acceptance_05_yahoo_filter() -> None:
    rows = [
        {
            "relatedTickers": tickers,
            "title": "Nvidia earnings",
            "link": f"https://fixture.example/{i}",
            "providerPublishTime": int(NOW.timestamp()),
        }
        for i, tickers in enumerate([["AMZN", "NVDA"], ["MU"], ["NVDA"]])
    ]
    with patch.object(src, "request", return_value=response({"news": rows})):
        assert len(src.fetch_yahoo("NVDA", "NVDA", NOW - timedelta(hours=48), NOW)) == 2


def test_acceptance_06_eastmoney_mapping() -> None:
    data = {
        "data": {
            "list": [
                {
                    "title": "Company announcement",
                    "notice_date": NOW.date().isoformat(),
                    "art_code": "AN1",
                    "codes": [{"stock_code": "00700", "short_name": "Tencent Chinese"}],
                }
            ]
        }
    }
    with patch.object(src, "request", return_value=response(data)) as get:
        result = src.fetch_eastmoney_ann("00700", "H", NOW - timedelta(hours=48), NOW)
    assert result[0].kind == "filing" and result[0].filing_form == "announcement"
    assert result[0].url == "https://data.eastmoney.com/notices/detail/00700/AN1.html"
    assert get.call_args.kwargs["params"]["stock_list"] == "00700"


def test_acceptance_15_public_request_parameters() -> None:
    with patch.object(src, "request", return_value=response({"news": []})) as get:
        src.fetch_yahoo("Nvidia", "NVDA", NOW - timedelta(hours=48), NOW)
    assert set(get.call_args.kwargs["params"]) == {"q", "quotesCount", "newsCount"}
    assert get.call_args.kwargs["params"]["q"] == "Nvidia"


def test_acceptance_30_google_exact_suffix() -> None:
    xml = f"""<rss version="2.0"><channel><item><title>Nvidia beats estimates - Reuters</title><link>https://fixture.example/one</link><pubDate>{NOW.strftime("%a, %d %b %Y %H:%M:%S %z")}</pubDate><source>Reuters</source></item><item><title>Reuters poll: chip demand rises</title><link>https://fixture.example/two</link><pubDate>{NOW.strftime("%a, %d %b %Y %H:%M:%S %z")}</pubDate><source>Reuters</source></item></channel></rss>"""
    with patch.object(src, "request", return_value=httpx.Response(200, text=xml)):
        result = src.fetch_google_news("Nvidia", "en-US", NOW - timedelta(hours=48), NOW)
    assert [x.title for x in result] == [
        "Nvidia beats estimates",
        "Reuters poll: chip demand rises",
    ]


def test_sec_live_atom_field_mapping() -> None:
    import time

    import feedparser

    entry = feedparser.FeedParserDict(
        {
            "title": "8-K - Company",
            "updated_parsed": time.gmtime(NOW.timestamp()),
            "items-desc": "item 8.01",
            "accession-number": "0001045810-26-000078",
            "link": "https://fixture.example/filing",
        }
    )
    src.reset_run_cache()
    with (
        patch.object(
            src,
            "request",
            side_effect=[
                response({"0": {"ticker": "NVDA", "cik_str": 1045810}}),
                httpx.Response(200, text="mock"),
            ],
        ),
        patch.object(
            feedparser, "parse", return_value=feedparser.FeedParserDict({"entries": [entry]})
        ),
    ):
        rows = src.fetch_sec_8k("NVDA", NOW - timedelta(hours=48), NOW)
    assert rows[0].title == "8-K Item 8.01 0001045810-26-000078"
    assert rows[0].filing_form == "8-K Item 8.01"


def test_acceptance_28_host_pacing() -> None:
    clock = [100.0]
    sent: dict[str, list[float]] = {}

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def get(client: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
        sent.setdefault(url, []).append(clock[0])
        return response([])

    with (
        patch.dict(src._LAST, {}, clear=True),
        patch.object(time, "monotonic", side_effect=lambda: clock[0]),
        patch.object(time, "sleep", side_effect=sleep),
        patch.object(httpx.Client, "get", autospec=True, side_effect=get),
    ):
        for host, interval in src._INTERVALS.items():
            for _ in range(3):
                src.request("https://" + host + "/fixture")
            times = sent["https://" + host + "/fixture"]
            assert all(b - a >= interval - 1e-8 for a, b in pairwise(times))


def test_acceptance_06_profile_chinese_name(db_session: Session) -> None:
    from app.models.intel import InstrumentProfile
    from app.services.instrument_profiles import resolve_profiles
    from app.services.instrument_universe import UniverseEntry

    data = [{"codes": [{"stock_code": "00700", "short_name": "Tencent Chinese"}]}]
    with (
        patch("app.services.instrument_profiles.load_entity_aliases", return_value={}),
        patch("app.services.instrument_profiles.yf.Ticker") as ticker,
        patch.object(src, "eastmoney_rows", return_value=data) as announcements,
    ):
        ticker.return_value.info = {"shortName": "Tencent Holdings Ltd"}
        entry = UniverseEntry("0700.HK", "0700.HK", "HK")
        assert resolve_profiles(db_session, [entry], now=NOW) == []
        profile = db_session.get(InstrumentProfile, entry.identifier)
        assert profile is not None and profile.name_zh == "Tencent Chinese"
        assert profile.name_en == "Tencent"
        resolve_profiles(db_session, [entry], now=NOW + timedelta(days=29))
        assert announcements.call_count == 1
        ticker.return_value.info = {}
        announcements.return_value = []
        assert resolve_profiles(db_session, [entry], now=NOW + timedelta(days=31))
        assert profile.name_resolved_at == NOW and profile.name_zh == "Tencent Chinese"


def test_issue_628_yfinance_prefers_long_name(db_session: Session) -> None:
    from app.models.intel import InstrumentProfile
    from app.services.instrument_profiles import resolve_profiles
    from app.services.instrument_universe import UniverseEntry

    with (
        patch("app.services.instrument_profiles.load_entity_aliases", return_value={}),
        patch("app.services.instrument_profiles.yf.Ticker") as ticker,
        patch(
            "app.core.config.get_settings",
            return_value=get_settings().model_copy(update={"FINNHUB_API_KEY": None}),
        ),
    ):
        ticker.return_value.info = {
            "shortName": "ASML Holding N.V. - New York Re",
            "longName": "ASML Holding N.V.",
        }
        entry = UniverseEntry("ASML", "ASML", "US")
        assert resolve_profiles(db_session, [entry], now=NOW) == []
    profile = db_session.get(InstrumentProfile, "ASML")
    assert profile is not None and profile.name_en == "ASML"


def test_issue_628_manual_alias_is_name_override(db_session: Session) -> None:
    from app.models.intel import InstrumentProfile
    from app.services.instrument_profiles import resolve_profiles
    from app.services.instrument_universe import UniverseEntry

    with patch("app.services.instrument_profiles.yf.Ticker") as ticker:
        entry = UniverseEntry("MU", "MU", "US")
        assert resolve_profiles(db_session, [entry], now=NOW) == []
        ticker.assert_not_called()
    profile = db_session.get(InstrumentProfile, "MU")
    assert profile is not None
    assert profile.name_en == "Micron"
    assert profile.name_source == "config"
    assert profile.aliases == ["Micron", "MU"]


def test_issue_628_invalidates_only_inconsistent_fresh_profiles(db_session: Session) -> None:
    from app.models.intel import InstrumentProfile
    from app.services.instrument_profiles import resolve_profiles
    from app.services.instrument_universe import UniverseEntry

    for identifier, name in {
        "MU": "Micron Technology,",
        "LITE": "Lumentum Holdings",
        "INTC": "Intel",
        "AVGO": "Broadcom",
    }.items():
        db_session.add(
            InstrumentProfile(
                identifier=identifier,
                market="US",
                name_en=name,
                name_source="config" if identifier == "INTC" else "yfinance",
                aliases=[name, identifier],
                name_resolved_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(days=1),
            )
        )
    db_session.flush()
    info = {
        "MU": {"longName": "Micron Technology,", "shortName": "Micron"},
        "LITE": {"longName": "Lumentum Holdings Inc.", "shortName": "Lumentum"},
    }
    entries = [UniverseEntry(identifier, identifier, "US") for identifier in info]
    entries.extend([UniverseEntry("INTC", "INTC", "US"), UniverseEntry("AVGO", "AVGO", "US")])
    with (
        patch("app.services.instrument_profiles.load_entity_aliases", return_value={}),
        patch("app.services.instrument_profiles.yf.Ticker") as ticker,
        patch(
            "app.core.config.get_settings",
            return_value=get_settings().model_copy(update={"FINNHUB_API_KEY": None}),
        ),
    ):
        ticker.side_effect = lambda symbol: type("Ticker", (), {"info": info.get(symbol, {})})()
        assert resolve_profiles(db_session, entries, now=NOW) == []
        assert ticker.call_count == 2
        mu = db_session.get(InstrumentProfile, "MU")
        lite = db_session.get(InstrumentProfile, "LITE")
        assert mu is not None and mu.name_en == "Micron Technology"
        assert lite is not None and lite.name_en == "Lumentum"
        ticker.reset_mock()
        assert resolve_profiles(db_session, entries, now=NOW) == []
        ticker.assert_not_called()


def test_issue_628_invalidates_fresh_profile_for_manual_alias_prefix(db_session: Session) -> None:
    from app.models.intel import InstrumentProfile
    from app.services.instrument_profiles import resolve_profiles
    from app.services.instrument_universe import UniverseEntry

    db_session.add(
        InstrumentProfile(
            identifier="MU",
            market="US",
            name_en="Micron Technology,",
            name_source="yfinance",
            aliases=["Micron Technology,", "MU"],
            name_resolved_at=NOW - timedelta(days=1),
            updated_at=NOW - timedelta(days=1),
        )
    )
    db_session.flush()
    entry = UniverseEntry("MU", "MU", "US")
    with patch("app.services.instrument_profiles.yf.Ticker") as ticker:
        assert resolve_profiles(db_session, [entry], now=NOW) == []
        ticker.assert_not_called()
        profile = db_session.get(InstrumentProfile, "MU")
        assert profile is not None
        assert profile.name_en == "Micron"
        assert profile.name_source == "config"
        assert profile.aliases == ["Micron", "MU"]
        assert resolve_profiles(db_session, [entry], now=NOW) == []
        ticker.assert_not_called()


def test_issue_628_d4_profiles_drive_matching(db_session: Session) -> None:
    from app.models.intel import InstrumentProfile
    from app.services.instrument_profiles import match_instruments, resolve_profiles
    from app.services.instrument_universe import UniverseEntry

    entries = [
        UniverseEntry(identifier, identifier, "US")
        for identifier in ["AMZN", "NVDA", "SPCX", "MU", "MUU"]
    ]
    with patch("app.services.instrument_profiles.yf.Ticker") as ticker:
        ticker.return_value.info = {}
        assert resolve_profiles(db_session, entries, now=NOW) == []
    profiles = {
        entry.identifier: db_session.get(InstrumentProfile, entry.identifier) for entry in entries
    }
    aliases = {identifier: profile.aliases for identifier, profile in profiles.items() if profile}
    assert match_instruments("Amazon seeks to offload $8bn of Nvidia chips", aliases) == {
        "AMZN",
        "NVDA",
    }
    assert match_instruments("SpaceX Stock Surges: What's Going On?", aliases) == {"SPCX"}
    assert match_instruments("Micron shares climb on HBM demand", aliases) == {"MU", "MUU"}


def test_acceptance_15_all_source_parameters_are_public() -> None:
    from app.models.intel import InstrumentProfile
    from app.services.instrument_news_capture import sources_for
    from app.services.instrument_universe import UniverseEntry

    captured: list[tuple[str, dict[str, str | int]]] = []

    def get(url: str, *, params: dict[str, str | int] | None = None) -> httpx.Response:
        captured.append((url, params or {}))
        if "company-news" in url:
            return response([])
        if "company_tickers" in url:
            return response({"0": {"ticker": "NVDA", "cik_str": 1045810}})
        if "finance.yahoo" in url:
            return response({"news": []})
        if "eastmoney" in url:
            return response({"data": {"list": []}})
        return httpx.Response(200, text='<rss version="2.0"><channel/></rss>')

    src.reset_run_cache()
    with (
        patch.object(src, "request", side_effect=get),
        patch.object(
            src,
            "get_settings",
            return_value=get_settings().model_copy(
                update={"FINNHUB_API_KEY": SecretStr("fixture-only")}
            ),
        ),
    ):
        for ticker, market in [
            ("NVDA", "US"),
            ("0700.HK", "HK"),
            ("600519.SS", "A-Share"),
            ("SAP.DE", "Europe"),
            ("7203.T", "Japan"),
            ("005930.KS", "Korea"),
            ("AZN.L", "UK"),
        ]:
            entry = UniverseEntry(ticker, ticker, market)
            profile = InstrumentProfile(
                identifier=ticker,
                market=market,
                name_en="Public Company",
                name_zh="Public Chinese",
                aliases=[ticker],
            )
            for _, fetch in sources_for(entry, profile, NOW - timedelta(hours=48), NOW):
                fetch()
    allowed = {
        "symbol",
        "from",
        "to",
        "token",
        "q",
        "hl",
        "gl",
        "ceid",
        "quotesCount",
        "newsCount",
        "action",
        "CIK",
        "type",
        "count",
        "output",
        "dateb",
        "page_size",
        "page_index",
        "ann_type",
        "stock_list",
        "sr",
        "client_source",
    }
    assert captured and all(set(params) <= allowed for _, params in captured)
    for _, params in captured:
        assert not any(
            k in params
            for k in [
                "user_id",
                "quantity",
                "value",
                "weight",
                "cost",
                "holder_count",
                "anomaly",
                "holding_name",
            ]
        )
    assert {key for _, params in captured for key in params} >= {"symbol", "q", "CIK", "ann_type"}


def test_acceptance_04_06_provider_leads_persist_without_metadata(db_session: Session) -> None:
    from sqlalchemy import select

    from app.models.intel import NewsInstrument
    from app.models.news import News
    from app.services import instrument_news_capture as cap
    from app.services.headline_cleaning import load_cleaning_config
    from app.services.instrument_universe import UniverseEntry
    from app.tests.test_instrument_news_capture import profile

    profile(db_session)
    profile(db_session, "0700.HK", "HK")
    rows = [
        {
            "datetime": int(NOW.timestamp()),
            "headline": title,
            "url": url,
            "source": "FixturePublisher",
            "summary": "Company update",
        }
        for title, url in [
            ("NVDA reports earnings above analyst expectations", "https://finnhub.io/one"),
            (
                "NVDA announces next generation processor architecture",
                "https://fixture.example/two",
            ),
        ]
    ]
    with patch.object(src, "request", return_value=response(rows)):
        articles = src.fetch_finnhub("NVDA", NOW - timedelta(hours=48), NOW)
    data = {
        "data": {
            "list": [
                {
                    "title": "Company announcement",
                    "notice_date": NOW.date().isoformat(),
                    "art_code": "AN1",
                    "codes": [{"stock_code": "00700", "short_name": "Tencent Chinese"}],
                }
            ]
        }
    }
    with patch.object(src, "request", return_value=response(data)):
        filings = src.fetch_eastmoney_ann("00700", "H", NOW - timedelta(hours=48), NOW)
    with (
        patch.object(
            cap,
            "sources_for",
            side_effect=[[("finnhub", lambda: articles)], [("eastmoney", lambda: filings)]],
        ),
        patch.object(
            cap, "classify_headlines", return_value=({0: "keep", 1: "mention"}, 0, None)
        ) as classifier,
    ):
        result = cap.collect_instrument_news(
            db_session, UniverseEntry("NVDA", "NVDA", "US"), NOW, load_cleaning_config()
        )
        cap.collect_instrument_news(
            db_session, UniverseEntry("0700.HK", "0700.HK", "HK"), NOW, load_cleaning_config()
        )
    assert classifier.call_count == 1
    assert [x.url_kind for x in result.leads] == ["finnhub_redirect", "direct"]
    stored = db_session.scalars(select(News)).all()
    assert len(stored) == 3 and len(db_session.scalars(select(NewsInstrument)).all()) == 3
    assert all(
        "https://" not in str(n.record) and "FixturePublisher" not in str(n.record) for n in stored
    )
    filing = next(n for n in stored if n.kind == "filing")
    assert filing.record["filing_form"] == "announcement"
    assert filing.url_hash == url_hash("https://data.eastmoney.com/notices/detail/00700/AN1.html")
