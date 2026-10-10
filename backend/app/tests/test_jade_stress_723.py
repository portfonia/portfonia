"""Issue #723 acceptance tests: local Postgres, fake providers only."""

import copy
import csv
import hashlib
import importlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import TextIO
from unittest.mock import Mock

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.jade_price import JadePricePoint, JadeScenarioPoint, JadeScenarioSeries
from app.models.user import User
from app.schemas.jade import JadeStressOut, StressScenario
from app.services import jade_price_history as history
from app.services import jade_replay_config as config
from app.services import jade_stress as stress
from app.services._yfinance import OhlcvPoint
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_jade_replay import E, calendar, holding, series
from app.tests.test_jade_stress import cached, compute, shock_fixture

IDS = ["dotcom_2000", "gfc_2008", "covid_2020", "rates_2022", "tariffs_2025"]
DP = date(2000, 3, 24)
DT = date(2002, 10, 9)
DS = [
    date(1999, 9, 24),
    *[DP - timedelta(days=i) for i in reversed(range(5))],
    *[DT + timedelta(days=i) for i in range(6)],
]
ROOT = Path(__file__).resolve().parents[2] / "config"


def substitutes() -> ModuleType:
    assert importlib.util.find_spec("app.services.jade_substitutes"), "missing substitute loader"
    return importlib.import_module("app.services.jade_substitutes")


def fred() -> ModuleType:
    assert importlib.util.find_spec("app.scripts.backfill_fx_fred"), "missing FRED backfill script"
    return importlib.import_module("app.scripts.backfill_fx_fred")


@pytest.fixture(autouse=True)
def setup(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_user(db_session, TEST_USER_ID)
    monkeypatch.setattr(stress, "today_et", lambda: E)
    guard = Mock(side_effect=AssertionError("real provider call"))
    monkeypatch.setattr(history, "fetch_ohlcv_range_bounded", guard)
    monkeypatch.setattr(history, "fetch_nav_history_pages", guard)
    monkeypatch.setattr(httpx, "get", guard)


def result(
    session: Session, sid: str = "dotcom_2000", benchmark: str = "sp500", base: str = "USD"
) -> StressScenario:
    return next(
        s
        for s in stress.compute_stress(session, TEST_USER_ID, base, benchmark).scenarios
        if s.id == sid
    )


def dot_calendar(session: Session) -> None:
    cached(session, "yf:SPY", DS, [100.0] * len(DS), "dotcom_2000")


def test_b1_config() -> None:
    assert [(s.id, s.start, s.end) for s in config.SCENARIOS] == [
        ("dotcom_2000", date(1999, 9, 24), date(2003, 4, 9)),
        ("gfc_2008", date(2007, 4, 9), date(2009, 9, 9)),
        ("covid_2020", date(2019, 8, 19), date(2020, 9, 23)),
        ("rates_2022", date(2021, 7, 3), date(2023, 4, 12)),
        ("tariffs_2025", date(2024, 8, 19), date(2025, 10, 8)),
    ]
    assert [s.fx_source for s in config.SCENARIOS] == [
        "fred",
        "fred",
        "standard",
        "standard",
        "standard",
    ]


def test_b2_substitutes() -> None:
    m = substitutes()
    assert set(m.SUBSTITUTES) == config.FIXED_ETF_SYMBOLS
    assert [(s.symbol, s.key) for s in m.candidates(config.CLASS_PROXY["BOND_FUND"])] == [
        ("AGG", "yf:AGG"),
        ("VBMFX", "yf:VBMFX"),
    ]
    assert len(m.substitute_keys()) == 13
    assert "file:LBMA_GOLD_PM" in m.substitute_keys()


@pytest.mark.parametrize(
    "case",
    [
        "document",
        "mapping",
        "missing_key",
        "extra_key",
        "non_list",
        "entry",
        "unknown_field",
        "missing_field",
        "symbol",
        "name",
        "source",
        "currency",
        "bool",
        "yf_file",
        "no_file",
        "path",
        "missing_file",
        "repeat",
        "own",
        "conflict",
    ],
)
def test_b3_validation(tmp_path: Path, case: str) -> None:
    m = substitutes()
    data = copy.deepcopy(yaml.safe_load((ROOT / "jade_substitutes.yml").read_text()))
    entries = data["substitutes"]
    entry = entries["AGG"][0]
    if case == "document":
        data = []
    elif case == "mapping":
        data["substitutes"] = []
    elif case == "missing_key":
        del entries["SPY"]
    elif case == "extra_key":
        entries["EXTRA"] = []
    elif case == "non_list":
        entries["AGG"] = None
    elif case == "entry":
        entries["AGG"] = ["bad"]
    elif case == "unknown_field":
        entry["extra"] = True
    elif case == "missing_field":
        del entry["name"]
    elif case == "symbol":
        entry["symbol"] = ""
    elif case == "name":
        entry["name"] = 7
    elif case == "source":
        entry["source"] = "unknown"
    elif case == "currency":
        entry["currency"] = "ZZZ"
    elif case == "bool":
        entry["price_only"] = "false"
    elif case == "yf_file":
        entry["file"] = "lbma_gold_pm.csv"
    elif case == "no_file":
        del entries["GLD"][0]["file"]
    elif case == "path":
        entries["GLD"][0]["file"] = "../lbma_gold_pm.csv"
    elif case == "missing_file":
        entries["GLD"][0]["file"] = "missing.csv"
    elif case == "repeat":
        entries["AGG"].append(copy.deepcopy(entry))
    elif case == "own":
        entry["symbol"] = "AGG"
    elif case == "conflict":
        entries["ACWI"][0]["currency"] = "EUR"
    path = tmp_path / "bad.yml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        m.load_substitutes(path)


def test_b4_gold() -> None:
    path = ROOT / "jade_scenario_data/lbma_gold_pm.csv"
    assert path.is_file(), "missing committed gold file"
    data = path.read_bytes()
    rows = list(csv.reader(data.decode().splitlines()))
    assert rows[0] == ["date", "close"]
    assert len(rows[1:]) == 896
    assert rows[1] == ["1999-09-14", "256.75"]
    assert rows[-1] == ["2003-04-09", "321.35"]
    assert ["2000-03-24", "284.85"] in rows
    assert ["2002-10-09", "319.35"] in rows
    assert rows[1:] == sorted(rows[1:])
    assert (
        hashlib.sha256(data).hexdigest()
        == "e53708d298c5c0ec7d081781795463211a7950d9b028ab7cb1a820f505b21fc8"
    )


def test_b5_file_fill(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    m = substitutes()
    original = Path.open
    reads: list[str] = []

    def observe(
        path: Path, mode: str = "r", *, encoding: str | None = None, newline: str | None = None
    ) -> TextIO:
        assert mode == "r"
        if path.name == "lbma_gold_pm.csv":
            reads.append(path.name)
        return original(path, "r", encoding=encoding, newline=newline)

    monkeypatch.setattr(Path, "open", observe)
    r = history.fill_scenarios(db_session, E, {"file:LBMA_GOLD_PM"})
    assert (r.attempted, r.written, r.failed) == (5, 1, 4)
    pts = list(db_session.scalars(select(JadeScenarioPoint).order_by(JadeScenarioPoint.trade_date)))
    assert len(pts) == 896 and {p.scenario_id for p in pts} == {"dotcom_2000"}
    assert pts[0].trade_date == date(1999, 9, 14) and pts[-1].trade_date == date(2003, 4, 9)
    assert all(p.raw_close is None for p in pts)
    pair = db_session.get(JadeScenarioSeries, ("dotcom_2000", "file:LBMA_GOLD_PM"))
    assert pair is not None and pair.last_success_on == E
    assert len(reads) == 5
    reads.clear()
    r = history.fill_scenarios(db_session, E, {"file:LBMA_GOLD_PM"})
    assert (r.attempted, r.written, r.failed) == (4, 0, 4)
    assert len(reads) == 4
    assert m.file_spec("file:LBMA_GOLD_PM").file == "lbma_gold_pm.csv"


def test_b5_nightly_keys(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    m = substitutes()
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_status, user.subscription_type = "active", "jade"
    db_session.flush()

    def yf(symbols: list[str], start: date, end: date) -> dict[str, list[OhlcvPoint]]:
        return {s: [(start, 100, 100, 100, 100, 1)] for s in symbols}

    batch = Mock(side_effect=yf)
    fill = Mock()
    monkeypatch.setattr(history, "fetch_ohlcv_range_bounded", batch)
    monkeypatch.setattr(history, "fill_scenarios", fill)
    summary = history.refresh_jade_price_history(db_session, E)
    primary = {"yf:" + s for s in config.FIXED_ETF_SYMBOLS}
    assert fill.call_args.args[2] == primary | m.substitute_keys()
    assert set(batch.call_args.args[0]) == config.FIXED_ETF_SYMBOLS
    assert {p.series_key for p in db_session.scalars(select(JadePricePoint))} == primary
    assert summary == history.FillSummary(len(primary), len(primary), 0)


def test_b5_substitute_batch(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    m = substitutes()
    batch = Mock(return_value={})
    monkeypatch.setattr(history, "fetch_ohlcv_range_bounded", batch)
    keys = {k for k in m.substitute_keys() if k.startswith("yf:")} | {"yf:SPY"}
    history.fill_scenarios(db_session, E, keys)
    assert len(batch.call_args_list) == 5
    for call, s in zip(batch.call_args_list, config.SCENARIOS, strict=True):
        assert set(call.args[0]) == {k[3:] for k in keys}
        assert call.args[1:] == (s.start - timedelta(days=10), s.end + timedelta(days=1))


@pytest.mark.parametrize(
    "kind,primary,chosen,asset,market,reason,price_only",
    [
        ("bond", "AGG", "VBMFX", "BOND_FUND", "US", None, False),
        ("late_efa", "EFA", "VGTSX", "EQUITY_DM", "US", None, False),
        ("gold", "GLD", "LBMA_GOLD_PM", "PRECIOUS_METALS", "US", None, False),
        ("hk", "2800.HK", "^HSI", "STOCK", "HK", None, True),
        ("primary_pending", "AGG", "VBMFX", "BOND_FUND", "US", "pending", False),
        ("sub_pending", "AGG", "VBMFX", "BOND_FUND", "US", "pending", False),
        ("no_coverage", "AGG", "VBMFX", "BOND_FUND", "US", "data_unavailable", False),
        ("missing_peak", "AGG", "VBMFX", "BOND_FUND", "US", None, False),
        ("missing_trough", "AGG", "VBMFX", "BOND_FUND", "US", None, False),
    ],
)
def test_b6_resolution(
    db_session: Session,
    kind: str,
    primary: str,
    chosen: str,
    asset: str,
    market: str,
    reason: str | None,
    price_only: bool,
) -> None:
    dot_calendar(db_session)
    primary_days = (
        [date(2001, 8, 1)]
        if kind == "late_efa"
        else [DT]
        if kind == "missing_peak"
        else [DP]
        if kind == "missing_trough"
        else []
    )
    cached(
        db_session,
        "yf:" + primary,
        primary_days,
        [100.0] * len(primary_days),
        "dotcom_2000",
        attempted=kind != "primary_pending",
    )
    substitute_days = [] if kind == "no_coverage" else DS
    cached(
        db_session,
        ("file:" if kind == "gold" else "yf:") + chosen,
        substitute_days,
        [100.0] * len(substitute_days),
        "dotcom_2000",
        attempted=kind != "sub_pending",
    )
    if market == "HK":
        db_session.add_all([FxRate(pair="USDHKD", rate_date=d, rate=Decimal(8)) for d in DS])
    holding(db_session, asset_class=asset, market=market)
    row = result(db_session).holdings[0]
    assert row.method == ("excluded" if reason else "proxy")
    assert row.excluded_reason == reason
    assert row.proxy_symbol == (primary if reason else chosen)
    assert row.proxy_name == (
        config.CLASS_PROXY[asset].name if reason else substitutes().SUBSTITUTES[primary][0].name
    )
    assert row.proxy_for == (None if reason else primary)
    assert row.price_only == price_only


def test_b7_chosen_fx_values(db_session: Session) -> None:
    dot_calendar(db_session)
    cached(db_session, "yf:2800.HK", [], [], "dotcom_2000")
    cached(db_session, "yf:^HSI", DS, [100.0] * 6 + [80.0] * 6, "dotcom_2000")
    db_session.add_all(
        [FxRate(pair="USDHKD", rate_date=d, rate=Decimal(8 if d < DT else 10)) for d in DS]
    )
    holding(db_session, market="HK")
    out = result(db_session)
    assert out.shock_return == "-0.360000"
    assert sum(Decimal(c.contribution) for c in out.contributions) == Decimal(out.shock_return)


def test_b7_head_beta_primary(db_session: Session) -> None:
    dot_calendar(db_session)
    cached(db_session, "yf:AGG", [], [], "dotcom_2000")
    cached(db_session, "yf:VBMFX", DS, [100.0] * 6 + [90.0] * 6, "dotcom_2000")
    cached(db_session, "yf:LATE", [DT, DT + timedelta(days=1)], [50.0, 55.0], "dotcom_2000")
    days = [E - timedelta(days=60 - i) for i in range(61)]
    calendar(db_session, days)
    p, own = [100.0], [50.0]
    for i in range(60):
        r = 0.01 if i % 2 else -0.01
        p.append(p[-1] * (1 + r))
        own.append(own[-1] * (1 + 2 * r))
    series(db_session, "yf:AGG", days, p)
    series(db_session, "yf:LATE", days, own)
    holding(db_session, auto=True, ticker="LATE", asset_class="BOND_FUND")
    out = result(db_session)
    assert (
        out.holdings[0].method,
        out.holdings[0].proxy_symbol,
        out.holdings[0].beta,
        out.holdings[0].beta_samples,
    ) == ("head_proxy", "VBMFX", "2.0000", 60)
    assert out.shock_return == "-0.200000"
    assert next(p for p in out.points if p.date == DT + timedelta(days=1)).portfolio == "-0.120000"


@pytest.mark.parametrize(
    "sid,benchmark,primary,symbol,currency",
    [
        ("dotcom_2000", "nasdaq", "ONEQ", "^IXIC", "USD"),
        ("gfc_2008", "csi300", "510300.SS", "000001.SS", "CNY"),
    ],
)
def test_b8_benchmark(
    db_session: Session, sid: str, benchmark: str, primary: str, symbol: str, currency: str
) -> None:
    scenario = next((s for s in config.SCENARIOS if s.id == sid), None)
    assert scenario is not None, "missing historical scenario"
    days = [
        scenario.start,
        *[scenario.peak - timedelta(days=i) for i in reversed(range(5))],
        *[scenario.trough + timedelta(days=i) for i in range(6)],
    ]
    cached(db_session, "yf:SPY", days, [100.0] * len(days), sid)
    cached(db_session, "yf:" + primary, [], [], sid)
    cached(db_session, "yf:" + symbol, days, [100.0] * 6 + [50.0] * 6, sid)
    if currency == "CNY":
        db_session.add_all([FxRate(pair="USDCNY", rate_date=d, rate=Decimal(7)) for d in days])
    holding(db_session, asset_type="cash")
    out = result(db_session, sid, benchmark)
    assert (
        out.benchmark_status,
        out.benchmark_symbol,
        out.benchmark_price_only,
        out.benchmark_shock_return,
    ) == ("ok", symbol, True, "-0.500000")
    assert [(s.primary, s.symbol) for s in out.substitutions] == [(primary, symbol)]
    assert out.price_index_symbols == [symbol]


def test_b8_pending_unavailable(db_session: Session) -> None:
    dot_calendar(db_session)
    holding(db_session, asset_type="cash")
    out = result(db_session, benchmark="nasdaq")
    assert (out.benchmark_status, out.benchmark_symbol) == ("pending", "ONEQ")
    cached(db_session, "yf:DIA", [DT], [100.0], "dotcom_2000")
    other = result(db_session, benchmark="dow30")
    assert (other.benchmark_status, other.benchmark_symbol) == ("unavailable", "DIA")
    assert (other.shock_return, other.max_drawdown) == (out.shock_return, out.max_drawdown)


def test_b9_disclosures(db_session: Session) -> None:
    dot_calendar(db_session)
    for primary, symbol in [
        ("2800.HK", "^HSI"),
        ("AGG", "VBMFX"),
        ("ONEQ", "^IXIC"),
        ("510300.SS", "000001.SS"),
    ]:
        cached(db_session, "yf:" + primary, [], [], "dotcom_2000")
        cached(db_session, "yf:" + symbol, DS, [100.0] * len(DS), "dotcom_2000")
    db_session.add_all(
        [
            FxRate(pair=pair, rate_date=d, rate=Decimal(7))
            for pair in ("USDHKD", "USDCNY")
            for d in DS
        ]
    )
    for i, (asset, market, value) in enumerate(
        [
            ("STOCK", "HK", "100"),
            ("BOND_FUND", "US", "100"),
            ("STOCK", "HK", "100"),
            ("EQUITY_CN", "A-Share", "100"),
            ("STOCK", "HK", "0"),
        ]
    ):
        h = holding(db_session, asset_class=asset, market=market, value=value)
        h.position = i
    db_session.flush()
    out = result(db_session, benchmark="nasdaq")
    assert [(s.primary, s.symbol) for s in out.substitutions] == [
        ("2800.HK", "^HSI"),
        ("AGG", "VBMFX"),
        ("510300.SS", "000001.SS"),
        ("ONEQ", "^IXIC"),
    ]
    assert out.price_index_symbols == ["000001.SS", "^HSI", "^IXIC"]
    # The benchmark uses the same series as the last included proxy: one pair only.
    again = result(db_session, benchmark="csi300")
    assert [(s.primary, s.symbol) for s in again.substitutions] == [
        ("2800.HK", "^HSI"),
        ("AGG", "VBMFX"),
        ("510300.SS", "000001.SS"),
    ]
    db_session.query(JadeScenarioPoint).filter(JadeScenarioPoint.series_key == "yf:^IXIC").delete()
    db_session.flush()
    unavailable = result(db_session, benchmark="nasdaq")
    assert ("ONEQ", "^IXIC") not in [(s.primary, s.symbol) for s in unavailable.substitutions]
    assert "^IXIC" not in unavailable.price_index_symbols


def test_b10_regression_baseline(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    shock_fixture(db_session)
    before = compute(db_session).model_dump()
    if importlib.util.find_spec("app.services.jade_substitutes"):
        m = substitutes()
        monkeypatch.setattr(m, "SUBSTITUTES", {s: () for s in config.FIXED_ETF_SYMBOLS})
    assert compute(db_session).model_dump() == before


def test_b10_new_fields(db_session: Session) -> None:
    shock_fixture(db_session)
    out = compute(db_session)
    assert (
        out.substitutions,
        out.price_index_symbols,
        out.benchmark_symbol,
        out.benchmark_name,
        out.benchmark_price_only,
        out.fx_source,
    ) == ([], [], "SPY", config.SPY.name, False, "standard")


def test_b11_fred(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    m = fred()
    from app.services.fx_fetcher import _PAIRS

    assert set(m.FRED_SERIES) | set(m.DERIVED) == set(_PAIRS)
    calls: list[tuple[str, dict[str, str]]] = []
    values = {"DEXUSEU": "1.0466", "DEXJPUS": "104.14", "DEXHKUS": "7.7675", "DEXCHUS": "8.2775"}

    def get(url: str, *, params: dict[str, str], timeout: int) -> httpx.Response:
        assert timeout == 30
        calls.append((url, params))
        value = values.get(params["id"], "1")
        return httpx.Response(
            200,
            text=f"observation_date,{params['id']}\n1999-09-24,{value}\n2000-01-03,.\n2000-01-04,\n2005-01-03,1\n2007-10-09,{value}\n",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", get)
    assert m.run(db_session, apply=False) == 0
    assert not list(db_session.scalars(select(FxRate)))
    assert len(calls) == 12
    assert all(c[1]["cosd"] == "1999-09-14" and c[1]["coed"] == "2009-09-09" for c in calls)
    assert m.run(db_session, apply=True) == 0
    rows = list(db_session.scalars(select(FxRate)))
    assert len(rows) == 28
    rates = {r.pair: r for r in rows if r.rate_date == date(1999, 9, 24)}
    assert rates["USDEUR"].rate == Decimal(1) / Decimal("1.0466")
    assert rates["USDJPY"].rate == Decimal("104.14")
    assert (rates["USDMOP"].rate, rates["USDMOP"].source) == (Decimal("8.000525"), "fred_hkd_peg")
    assert (rates["USDCNH"].rate, rates["USDCNH"].source) == (Decimal("8.2775"), "fred_cny")
    assert rates["USDCNY"].rate == Decimal("8.2775")
    assert rates["USDHKD"].rate == Decimal("7.7675")
    row = rates["USDEUR"]
    row.rate, row.source, row.fetched_at = (
        Decimal(".8"),
        "existing",
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.commit()
    snapshot = (row.rate, row.source, row.fetched_at)
    assert m.run(db_session, apply=True) == 0
    db_session.refresh(row)
    assert (row.rate, row.source, row.fetched_at) == snapshot
    assert (
        "check USDEUR 1999-09-24: fred=0.9554748710108924135295241735 stored=0.8 diff="
        in capsys.readouterr().out
    )


def test_b11_failure(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    m = fred()

    def get(url: str, *, params: dict[str, str], timeout: int) -> httpx.Response:
        if params["id"] == "DEXHKUS":
            raise httpx.ConnectError("offline")
        return httpx.Response(
            200,
            text=f"observation_date,{params['id']}\n1999-09-24,1\n",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", get)
    assert m.run(db_session, apply=True) == 1
    pairs = set(db_session.scalars(select(FxRate.pair)))
    assert "USDHKD" not in pairs and "USDMOP" not in pairs
    assert "USDEUR" in pairs
    assert "[ERR] DEXHKUS: offline" in capsys.readouterr().out


def test_b12_api(app_client: TestClient, db_session: Session) -> None:
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_status, user.subscription_type = "active", "jade"
    db_session.flush()
    response = app_client.get("/jade/stress")
    assert response.status_code == 200
    out = JadeStressOut.model_validate(response.json())
    assert [s.id for s in out.scenarios] == IDS
    assert [s.fx_source for s in out.scenarios] == [
        "fred",
        "fred",
        "standard",
        "standard",
        "standard",
    ]
