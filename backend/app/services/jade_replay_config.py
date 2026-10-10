"""Issue #714 proxy table and replay constants."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

ReplayRange = Literal["1M", "3M", "6M", "YTD", "1Y", "3Y", "5Y"]
REPLAY_RANGES: tuple[ReplayRange, ...] = ("1M", "3M", "6M", "YTD", "1Y", "3Y", "5Y")
DEFAULT_REPLAY_RANGE: ReplayRange = "1Y"

REPLAY_YEARS = 5
FETCH_MARGIN_DAYS = 15
CARRY_DAYS = 10
MIN_RETURNS = 10
BETA_MIN_SAMPLES = 60
DATA_QUALITY_SHARE = Decimal("0.66")
NAV_PAGE_SIZE = 20
NAV_PAGE_PAUSE_SECONDS = 0.2


@dataclass(frozen=True)
class EtfSpec:
    symbol: str
    currency: str
    name: str


SPY = EtfSpec("SPY", "USD", "SPDR S&P 500 ETF Trust")
CSI = EtfSpec("510300.SS", "CNY", "Huatai-PineBridge CSI 300 ETF")
ACWI = EtfSpec("ACWI", "USD", "iShares MSCI ACWI ETF")
STOCK_PROXY_BY_MARKET = {
    "US": SPY,
    "HK": EtfSpec("2800.HK", "HKD", "Tracker Fund of Hong Kong"),
    "A-Share": CSI,
    "UK": EtfSpec("ISF.L", "GBP", "iShares Core FTSE 100 UCITS ETF"),
    "Europe": EtfSpec("EXSA.DE", "EUR", "iShares STOXX Europe 600 UCITS ETF (DE)"),
    "Japan": EtfSpec("1306.T", "JPY", "NEXT FUNDS TOPIX ETF"),
    "Korea": EtfSpec("069500.KS", "KRW", "KODEX 200"),
    "Other": ACWI,
}
CLASS_PROXY = {
    "EQUITY_US_BROAD": SPY,
    "EQUITY_US_TECH": EtfSpec("QQQ", "USD", "Invesco QQQ Trust"),
    "EQUITY_DM": EtfSpec("EFA", "USD", "iShares MSCI EAFE ETF"),
    "EQUITY_CN": CSI,
    "EQUITY_EM": EtfSpec("EEM", "USD", "iShares MSCI Emerging Markets ETF"),
    "EQUITY_BROAD": ACWI,
    "REIT": EtfSpec("VNQ", "USD", "Vanguard Real Estate ETF"),
    "PRECIOUS_METALS": EtfSpec("GLD", "USD", "SPDR Gold Shares"),
    "ENERGY": EtfSpec("XLE", "USD", "Energy Select Sector SPDR Fund"),
    "COMMODITY": EtfSpec("DBC", "USD", "Invesco DB Commodity Index Tracking Fund"),
    "BOND_FUND": EtfSpec("AGG", "USD", "iShares Core U.S. Aggregate Bond ETF"),
}
BENCHMARK_ETF = {
    "sp500": SPY,
    "csi300": CSI,
    "nasdaq": EtfSpec("ONEQ", "USD", "Fidelity Nasdaq Composite Index ETF"),
    "dow30": EtfSpec("DIA", "USD", "SPDR Dow Jones Industrial Average ETF Trust"),
}
FIXED_ETF_SYMBOLS = {
    e.symbol
    for e in [*STOCK_PROXY_BY_MARKET.values(), *CLASS_PROXY.values(), *BENCHMARK_ETF.values()]
}


STYLE_BASIS: tuple[EtfSpec, ...] = (
    EtfSpec("IWF", "USD", "iShares Russell 1000 Growth ETF"),
    EtfSpec("IWD", "USD", "iShares Russell 1000 Value ETF"),
    EtfSpec("IWM", "USD", "iShares Russell 2000 ETF"),
    CLASS_PROXY["EQUITY_DM"],
    CLASS_PROXY["EQUITY_EM"],
    STOCK_PROXY_BY_MARKET["HK"],
    CSI,
    CLASS_PROXY["BOND_FUND"],
    EtfSpec("TLT", "USD", "iShares 20+ Year Treasury Bond ETF"),
    CLASS_PROXY["PRECIOUS_METALS"],
    CLASS_PROXY["COMMODITY"],
    CLASS_PROXY["REIT"],
    EtfSpec("BIL", "USD", "SPDR Bloomberg 1-3 Month T-Bill ETF"),
)
STYLE_KEYS = frozenset("yf:" + e.symbol for e in STYLE_BASIS)
STYLE_RANGE: ReplayRange = "3M"
STYLE_HORIZON_DAYS = 3
STYLE_MIN_RETURNS = 42
STYLE_LOW_FIT = 0.6
STYLE_SOLVER_TOL = 1e-12


def years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def months_before(day: date, months: int) -> date:
    """Same day earlier by calendar months, clamped to the month end."""
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


TAIL_LEVELS = (95, 99)
MIN_RETURNS_99 = 500
MONTH_DAYS = 21
TRADING_DAYS = 252
HIST_BIN = Decimal("0.005")

ScenarioId = Literal["dotcom_2000", "gfc_2008", "covid_2020", "rates_2022", "tariffs_2025"]
FxSource = Literal["standard", "fred"]
SCENARIO_MONTHS = 6
CALENDAR_KEY = "yf:SPY"


def months_after(day: date, months: int) -> date:
    return months_before(day, -months)


@dataclass(frozen=True)
class Scenario:
    id: ScenarioId
    peak: date
    trough: date
    fx_source: FxSource = "standard"

    @property
    def start(self) -> date:
        return months_before(self.peak, SCENARIO_MONTHS)

    @property
    def end(self) -> date:
        return months_after(self.trough, SCENARIO_MONTHS)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("dotcom_2000", date(2000, 3, 24), date(2002, 10, 9), "fred"),
    Scenario("gfc_2008", date(2007, 10, 9), date(2009, 3, 9), "fred"),
    Scenario("covid_2020", date(2020, 2, 19), date(2020, 3, 23)),
    Scenario("rates_2022", date(2022, 1, 3), date(2022, 10, 12)),
    Scenario("tariffs_2025", date(2025, 2, 19), date(2025, 4, 8)),
)
