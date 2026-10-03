from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.price_snapshot import PriceSnapshot
from app.services.instrument_universe import UniverseEntry
from app.services.intel_body import body_verdict, clean_body
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_leads import url_key
from app.services.intel_selection import assign_providers, select_units
from app.services.intel_signals import Signal, compute_signals, spike

NOW = datetime(2026, 10, 2, 16, 15, tzinfo=ET)


def test_01_captured_signals(db_session: Session) -> None:
    series: dict[str, list[float]] = {
        "AAA": [100, 100, 100, 100, 100, 93.8],
        "BBB": [100, 100, 100, 110, 120, 121.4],
        "CCC": [100, 100, 100, 103, 106, 109.1],
        "EEE": [100, 101],
    }
    for ticker, values in series.items():
        for i, value in enumerate(values):
            db_session.add(
                PriceSnapshot(
                    ticker=ticker,
                    market="US",
                    session_node="close",
                    trade_date=NOW.date() - timedelta(days=len(values) - i - 1),
                    close=Decimal(str(value)),
                )
            )
    db_session.flush()
    signals = compute_signals(
        db_session,
        [UniverseEntry(t, t, "US") for t in series],
        NOW.date(),
        NOW - timedelta(hours=24),
        load_intel_deepen_config(),
        slot="post_close",
        now=NOW,
    )
    assert signals["AAA"].strength == pytest.approx(0.062)
    assert signals["AAA"].direction == "down"
    assert signals["BBB"].strength == pytest.approx(0.214)
    assert signals["BBB"].direction == "up"
    assert signals["CCC"].near
    assert signals["EEE"].d3 is signals["EEE"].d5 is None
    assert not signals["EEE"].mover


def test_02_fixed_single_day_config(tmp_path: Path) -> None:
    cfg = load_intel_deepen_config()
    assert cfg.thresholds.single_day == 0.05
    assert not Signal.from_closes(
        "AAA",
        [(NOW.date(), 98), (NOW.date() - timedelta(days=1), 100)],
        cfg,
        NOW.date(),
        "post_close",
        NOW - timedelta(hours=24),
    ).mover
    data = cfg.model_dump()
    data["thresholds"]["single_day"] = 0.07
    path = tmp_path / "intel_deepen.yml"
    path.write_text(yaml.safe_dump(data))
    cfg = load_intel_deepen_config(path)
    signal = Signal.from_closes(
        "AAA",
        [(NOW.date(), 93.8), (NOW.date() - timedelta(days=1), 100)],
        cfg,
        NOW.date(),
        "post_close",
        NOW - timedelta(hours=24),
    )
    assert not signal.mover


def test_03_news_spike_history() -> None:
    cfg = load_intel_deepen_config()
    assert spike(8, [2, 3, 2, 3, 2], cfg, False)
    assert not spike(5, [2, 3, 2, 3, 2], cfg, False)
    assert not spike(8, [2, 3, 2, 3], cfg, False)


def test_04_selection_caps_and_tiers() -> None:
    cfg = load_intel_deepen_config()
    signals = {
        str(i): Signal(str(i), mover=True, strength=i / 100, window_start=NOW.date())
        for i in range(25)
    }
    signals.update(
        {
            "near": Signal("near", near=True, strength=1, window_start=NOW.date()),
            "filing": Signal("filing", filings=1, window_start=NOW.date()),
            "spike": Signal("spike", news_spike=True, strength=3, window_start=NOW.date()),
        }
    )
    units = select_units(signals, {}, cfg)
    assert len([u for u in units if u.kind == "mover"]) == 20
    assert units[0].identifier == "24"
    assert [u.identifier for u in units[-3:]] == ["near", "filing", "spike"]


def test_05_provider_assignment() -> None:
    cfg = load_intel_deepen_config()
    signals = {
        "BBB": Signal("BBB", mover=True, strength=0.214),
        "AAA": Signal("AAA", mover=True, strength=0.062),
        "CCC": Signal("CCC", near=True),
        "DDD": Signal("DDD", filings=1),
    }
    units = select_units(signals, {"energy": 14, "rates": 9}, cfg)
    assigned = assign_providers(units, "ab", {"tavily", "parallel"}, cfg)
    assert [u.providers for u in assigned] == [
        ("tavily", "parallel"),
        ("tavily", "parallel"),
        ("tavily",),
        ("parallel",),
        ("tavily",),
        ("parallel",),
    ]
    assert all(u.providers == ("tavily",) for u in assign_providers(units, "ab", {"tavily"}, cfg))
    assert all(
        u.providers == ("parallel",)
        for u in assign_providers(units, "parallel", {"tavily", "parallel"}, cfg)
    )


def test_09_url_normalization() -> None:
    assert url_key("https://Ex.com/a?utm_source=x&id=1#frag") == url_key("https://ex.com/a?id=1")


def test_10_body_verdict() -> None:
    cfg = load_intel_deepen_config()
    assert body_verdict("x" * 280, cfg) == (False, "too_short")
    assert body_verdict("x" * 400 + "Subscribe to continue", cfg) == (False, "paywall")
    assert body_verdict("Copyright " + "x" * 600 + "\n" + "y" * 400, cfg) == (False, "boilerplate")
    cleaned = clean_body(
        "https://finance.yahoo.com/a", "Most active\n[...]\n" + "article " * 150 + ".", cfg
    )
    assert "Most active" not in cleaned
    assert body_verdict(cleaned, cfg) == (True, None)


def test_20_weekend_macro_rule() -> None:
    cfg = load_intel_deepen_config()
    assert not select_units({}, {"energy": 9}, cfg, weekend=True)
    assert select_units(
        {}, {"energy": 14}, cfg, weekend=True, theme_history={"energy": [3, 4, 4, 5, 6]}
    )
    assert not select_units(
        {}, {"energy": 14}, cfg, weekend=True, theme_history={"energy": [10, 12, 14, 15, 16]}
    )


def test_25_body_url_removal() -> None:
    cfg = load_intel_deepen_config()
    text = (
        "Reuters reports an agreement. " * 40
        + "[Read more](https://example.com/x) https://example.com/y"
    )
    cleaned = clean_body("https://example.com/a", text, cfg)
    assert "Read more" in cleaned and "Reuters" in cleaned and "http" not in cleaned
    assert body_verdict(cleaned, cfg)[0]
    assert body_verdict(
        clean_body(
            "https://example.com/a",
            "Agreement terms will support a new factory and expand production capacity this year. "
            * 3
            + " https://example.com/"
            + "y" * 100,
            cfg,
        ),
        cfg,
    ) == (False, "too_short")


def test_26_quiet_windows() -> None:
    cfg = load_intel_deepen_config()
    closes = [
        (NOW.date() - timedelta(days=i), v) for i, v in enumerate([109.1, 109, 108, 107, 106, 100])
    ]
    signal = Signal.from_closes(
        "AAA", closes, cfg, NOW.date(), "post_close", NOW - timedelta(hours=24)
    )
    assert signal.near and signal.window_start == closes[5][0]
    signal = Signal("DDD", filings=1, window_start=(NOW - timedelta(hours=24)).date())
    assert (
        select_units({"DDD": signal}, {}, cfg)[0].window_start == (NOW - timedelta(hours=24)).date()
    )


def test_27_config_validation(tmp_path: object) -> None:
    path = Path(str(tmp_path)) / "intel.yml"
    data = load_intel_deepen_config().model_dump()
    assert data["thresholds"]["news_spike_ratio"] == 2
    data["extract"]["max_boilerplate_ratio"] = 0
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        load_intel_deepen_config(path)


def test_03_fresh_associations_and_recorded_history(db_session: Session) -> None:
    from app.models.intel import IntelSlotRun, NewsInstrument
    from app.models.news import News

    cfg = load_intel_deepen_config()
    previous = NOW - timedelta(hours=24)
    for i, count in enumerate([2, 3, 2, 3, 2]):
        day = NOW - timedelta(days=i + 1)
        db_session.add(
            IntelSlotRun(
                slot="post_close",
                run_date=day.date(),
                started_at=day,
                status="ok",
                details={"fresh_counts": {"AAA": count}, "universe": ["AAA"]},
            )
        )
    for i in range(9):
        news = News(
            url_hash=f"fresh{i}",
            record={"title": "AAA agreement"},
            published_at=NOW,
            fetched_at=NOW,
        )
        db_session.add(news)
        db_session.flush()
        db_session.add(
            NewsInstrument(news_id=news.id, identifier="AAA", created_at=NOW if i < 8 else previous)
        )
    db_session.flush()
    signal = compute_signals(
        db_session,
        [UniverseEntry("AAA", "AAA", "US")],
        NOW.date(),
        previous,
        cfg,
        slot="post_close",
        now=NOW,
    )["AAA"]
    assert signal.fresh == 8 and signal.news_spike
