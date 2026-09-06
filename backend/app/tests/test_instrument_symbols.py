"""Golden-fixture parity for `instrument_symbols.normalize_legacy_ticker`.

Asserts that the extracted implementation reproduces, byte-for-byte, outputs
recorded from the pre-#57 `_yfinance._normalize_ticker` implementation (see
the fixture file's own `_comment`/`source_head`). Stage 57-3 removed the
`_yfinance._normalize_ticker` forwarding shim and the `_TICKER_SYMBOL_
OVERRIDE` re-export this file used to also exercise — `normalize_legacy_
ticker` is now the only implementation, so there is nothing left to compare
it against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
