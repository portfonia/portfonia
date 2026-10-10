"""Validated scenario substitute chains for replay proxy ETFs (issue #723)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml

from app.schemas.holdings import VALID_CURRENCIES
from app.services.jade_replay_config import FIXED_ETF_SYMBOLS, EtfSpec

SeriesSource = Literal["yf", "file"]
DATA_DIR = Path(__file__).resolve().parents[2] / "config/jade_scenario_data"
_PATH = Path(__file__).resolve().parents[2] / "config/jade_substitutes.yml"


@dataclass(frozen=True)
class SeriesSpec:
    symbol: str
    currency: str
    name: str
    source: SeriesSource = "yf"
    file: str | None = None
    price_only: bool = False

    @property
    def key(self) -> str:
        return f"{self.source}:{self.symbol}"


def load_substitutes(path: Path | None = None) -> dict[str, tuple[SeriesSpec, ...]]:
    data = yaml.safe_load((path or _PATH).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("document must be a mapping")
    raw = data.get("substitutes")
    if not isinstance(raw, dict):
        raise ValueError("substitutes must be a mapping")
    if set(raw) != FIXED_ETF_SYMBOLS:
        raise ValueError(
            f"substitutes: missing {FIXED_ETF_SYMBOLS - set(raw)}, extra {set(raw) - FIXED_ETF_SYMBOLS}"
        )
    result: dict[str, tuple[SeriesSpec, ...]] = {}
    known: dict[str, SeriesSpec] = {}
    required = {"symbol", "source", "currency", "name", "price_only"}
    for primary, entries in raw.items():
        if not isinstance(entries, list):
            raise ValueError(f"{primary}: substitutes must be a list")
        chain = []
        seen = {primary}
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not required <= set(entry)
                or set(entry) - (required | {"file"})
            ):
                raise ValueError(f"{primary}: invalid entry fields")
            symbol, name, source, currency, price_only = (
                entry[k] for k in ("symbol", "name", "source", "currency", "price_only")
            )
            if (
                not isinstance(symbol, str)
                or not symbol.strip()
                or not isinstance(name, str)
                or not name.strip()
                or source not in ("yf", "file")
                or not isinstance(currency, str)
                or currency not in VALID_CURRENCIES
                or not isinstance(price_only, bool)
            ):
                raise ValueError(f"{primary}: invalid series fields")
            filename = entry.get("file")
            if source == "yf" and "file" in entry:
                raise ValueError(f"{primary}: yf series cannot have a file")
            if source == "file" and (
                not isinstance(filename, str)
                or not filename
                or filename in (".", "..")
                or Path(filename).name != filename
                or "\\" in filename
                or not (DATA_DIR / filename).is_file()
            ):
                raise ValueError(f"{primary}: invalid or missing data file")
            spec = SeriesSpec(
                symbol, currency, name, cast(SeriesSource, source), filename, price_only
            )
            if symbol in seen:
                raise ValueError(f"{primary}: repeated or primary symbol {symbol}")
            if symbol in known and known[symbol] != spec:
                raise ValueError(f"{primary}: conflicting specification for {symbol}")
            seen.add(symbol)
            known[symbol] = spec
            chain.append(spec)
        result[primary] = tuple(chain)
    return result


SUBSTITUTES = load_substitutes()


def candidates(spec: EtfSpec) -> tuple[SeriesSpec, ...]:
    return (SeriesSpec(spec.symbol, spec.currency, spec.name), *SUBSTITUTES[spec.symbol])


def substitute_keys() -> set[str]:
    return {s.key for chain in SUBSTITUTES.values() for s in chain}


def file_spec(key: str) -> SeriesSpec | None:
    return next(
        (s for chain in SUBSTITUTES.values() for s in chain if s.source == "file" and s.key == key),
        None,
    )
