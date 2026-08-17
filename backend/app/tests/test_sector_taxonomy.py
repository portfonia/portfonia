"""Unit tests for the sector taxonomy mapping (no DB, no network)."""

from __future__ import annotations

import pytest

from app.services.sector_taxonomy import (
    COMMUNICATION,
    CONSUMER_DISCRETIONARY,
    CONSUMER_STAPLES,
    FINANCIALS,
    MATERIALS,
    OTHER,
    TECHNOLOGY,
    map_yf_sector,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Technology", TECHNOLOGY),
        ("technology", TECHNOLOGY),
        ("  Technology  ", TECHNOLOGY),
        ("Communication Services", COMMUNICATION),
        ("Financial Services", FINANCIALS),
        ("Consumer Cyclical", CONSUMER_DISCRETIONARY),
        ("Consumer Defensive", CONSUMER_STAPLES),
        ("Basic Materials", MATERIALS),
    ],
)
def test_known_sectors_map(raw: str, expected: str) -> None:
    assert map_yf_sector(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "Crypto", "未知行业"])
def test_unknown_and_empty_fall_through_to_other(raw: str | None) -> None:
    assert map_yf_sector(raw) == OTHER


def test_valid_sectors_covers_every_declared_class() -> None:
    """`VALID_SECTORS` (issue #128 A3) is the closed set the L2 shared cache
    validates an LLM-proposed sector against. It is derived from the mapping
    table, so a class constant declared but never mapped would be silently
    unreachable — and an event legitimately classified into it would be
    dropped as "out of taxonomy"."""
    from app.services import sector_taxonomy as st

    declared = {
        value
        for name, value in vars(st).items()
        if name.isupper() and not name.startswith("_") and isinstance(value, str)
    }
    assert declared == set(st.VALID_SECTORS)
