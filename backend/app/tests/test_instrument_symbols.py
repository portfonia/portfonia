"""Golden-fixture parity for the #57 stage 57-1 extraction.

Asserts that `instrument_symbols.normalize_legacy_ticker` and the
`_yfinance._normalize_ticker` forwarding shim both reproduce, byte-for-byte,
outputs recorded from the pre-refactor `_yfinance._normalize_ticker`
implementation (see the fixture file's own `_comment`/`source_head`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import _yfinance
from app.services.instrument_symbols import normalize_legacy_ticker

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "legacy_ticker_normalization_golden.json"


def _load_cases() -> list[dict[str, str]]:
    data: dict[str, object] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert isinstance(cases, list)
    return cases


_CASES = _load_cases()


@pytest.mark.parametrize("case", _CASES, ids=[repr(c["input"]) for c in _CASES])
def test_normalize_legacy_ticker_matches_golden_fixture(case: dict[str, str]) -> None:
    assert normalize_legacy_ticker(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", _CASES, ids=[repr(c["input"]) for c in _CASES])
def test_yfinance_shim_matches_golden_fixture(case: dict[str, str]) -> None:
    assert _yfinance._normalize_ticker(case["input"]) == case["expected"]


def test_yfinance_shim_delegates_to_instrument_symbols() -> None:
    assert _yfinance._normalize_ticker is not normalize_legacy_ticker
    assert _yfinance._normalize_ticker("PSH") == normalize_legacy_ticker("PSH")


def test_yfinance_reexports_ticker_symbol_override() -> None:
    from app.services.instrument_symbols import _TICKER_SYMBOL_OVERRIDE

    assert _yfinance._TICKER_SYMBOL_OVERRIDE is _TICKER_SYMBOL_OVERRIDE
