"""Unit tests for the sector taxonomy label set (no DB, no network).

Issue #435 removed the yfinance-mapping half of this module
(`_YF_SECTOR_MAP`/`map_yf_sector`) along with its only consumer
(`price_fetcher.backfill_sectors`). What remains is tested here: the closed
label set `questionnaire_taxonomy.py` validates the `sectors_of_interest`
questionnaire field against.
"""

from __future__ import annotations

from app.services import sector_taxonomy as st


def test_valid_sectors_covers_every_declared_class() -> None:
    """`VALID_SECTORS` must stay in sync with the module's own class
    constants — a constant declared but missing from the set would be
    silently unreachable for `sectors_of_interest` validation."""
    declared = {
        value
        for name, value in vars(st).items()
        if name.isupper() and not name.startswith("_") and isinstance(value, str)
    }
    assert declared == set(st.VALID_SECTORS)


def test_valid_sectors_includes_other_fallback() -> None:
    assert st.OTHER in st.VALID_SECTORS
