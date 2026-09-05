"""Integration tests for fx_fetcher — real Postgres, mocked yfinance."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.fx_rate import FxRate
from app.services import fx_fetcher

# 16:00 ET on 2026-06-04 → rate_date 2026-06-04 (well clear of midnight).
_AS_OF = datetime(2026, 6, 4, 20, 0, tzinfo=UTC)


@pytest.fixture
def production_env() -> Generator[None, None, None]:
    """issue #354 follow-up: FX ops alerts are gated on APP_ENV=="production"
    (same field/pattern as db_backup.py) so a local dev DB's permanently
    stale fx_rates (no Celery beat runs there) doesn't send real alerts to
    the admin inbox. Tests that assert an alert fires must opt into this —
    same cache_clear()-wrapped patch.dict pattern as test_db_backup.py."""
    get_settings.cache_clear()
    with patch.dict("os.environ", {"APP_ENV": "production"}):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


def _fake_points() -> dict[str, tuple[float, datetime]]:
    return {yf_ticker: (7.18, _AS_OF) for yf_ticker in fx_fetcher._PAIRS.values()}


def test_upsert_writes_all_pairs(db_session: Session) -> None:
    with patch.object(fx_fetcher, "fetch_last_close", return_value=_fake_points()):
        result = fx_fetcher.update_fx_rates(db_session)

    assert result.upserted == len(fx_fetcher._PAIRS)
    assert result.failed == []

    rows = {r.pair: r for r in db_session.execute(select(FxRate)).scalars()}
    assert set(rows) == set(fx_fetcher._PAIRS)
    assert rows["USDCNY"].rate == Decimal(str(7.18))
    assert rows["USDCNY"].rate_date == date(2026, 6, 4)


def test_upsert_is_idempotent(db_session: Session) -> None:
    with patch.object(fx_fetcher, "fetch_last_close", return_value=_fake_points()):
        fx_fetcher.update_fx_rates(db_session)
        fx_fetcher.update_fx_rates(db_session)

    count = len(list(db_session.execute(select(FxRate)).scalars()))
    assert count == len(fx_fetcher._PAIRS)  # second run updates in place, no duplicates


def test_no_data_marks_all_failed(db_session: Session) -> None:
    with patch.object(fx_fetcher, "fetch_last_close", return_value={}):
        result = fx_fetcher.update_fx_rates(db_session)

    assert result.upserted == 0
    assert set(result.failed) == set(fx_fetcher._PAIRS)


def test_partial_data_records_missing_pair(db_session: Session) -> None:
    points = {"USDCNY=X": (7.18, _AS_OF)}
    with patch.object(fx_fetcher, "fetch_last_close", return_value=points):
        result = fx_fetcher.update_fx_rates(db_session)

    assert result.upserted == 1
    assert set(result.failed) == set(fx_fetcher._PAIRS) - {"USDCNY"}


def test_pairs_cover_every_valid_currency_except_usd() -> None:
    """issue #204: GBP (and 10 other VALID_CURRENCIES entries) had no FX pair,
    so holdings in those currencies silently dropped out of every report
    total regardless of price correctness. Pin the full set so a future
    currency addition to VALID_CURRENCIES can't reintroduce the same gap."""
    from app.schemas.holdings import VALID_CURRENCIES

    assert {pair[3:] for pair in fx_fetcher._PAIRS} == VALID_CURRENCIES - {"USD"}


# ---------------------------------------------------------------------------
# issue #354 follow-up: alerts gated on APP_ENV=="production"
# ---------------------------------------------------------------------------


def test_no_alert_sent_outside_production(db_session: Session) -> None:
    """A local dev Postgres is never kept fresh (no Celery beat runs there),
    so a total fetch failure/every pair reading as stale is the DB's normal,
    expected state — real alerts would spam the admin inbox for a condition
    that isn't an incident. get_settings().APP_ENV defaults to
    "development" (no production_env fixture requested here)."""
    get_settings.cache_clear()
    with (
        patch.object(fx_fetcher, "fetch_last_close", return_value={}),
        patch.object(fx_fetcher, "send_ops_alert", return_value=True) as mock_alert,
    ):
        fx_fetcher.update_fx_rates(db_session)

    assert mock_alert.call_count == 0


# ---------------------------------------------------------------------------
# issue #354 item 7(a): per-pair fetch-failure ops alert
# ---------------------------------------------------------------------------


def test_total_fetch_failure_sends_ops_alert(db_session: Session, production_env: None) -> None:
    """Previously a 100%-fetch failure only logged an ERROR — nothing ever
    reached the ops inbox. An empty fx_rates table also trips every pair's
    "never resolved" gap check (item 7b) in the same run — a separate, both-
    real failure mode, not a duplicate of this one."""
    with (
        patch.object(fx_fetcher, "fetch_last_close", return_value={}),
        patch.object(fx_fetcher, "send_ops_alert", return_value=True) as mock_alert,
    ):
        fx_fetcher.update_fx_rates(db_session)

    fetch_failed_alert = next(
        c for c in mock_alert.call_args_list if "FX fetch failed" in c.kwargs["subject"]
    )
    assert "USDCNY" in fetch_failed_alert.kwargs["body"]


def test_partial_fetch_failure_sends_ops_alert(db_session: Session, production_env: None) -> None:
    points = {"USDCNY=X": (7.18, _AS_OF)}
    with (
        patch.object(fx_fetcher, "fetch_last_close", return_value=points),
        patch.object(fx_fetcher, "send_ops_alert", return_value=True) as mock_alert,
    ):
        fx_fetcher.update_fx_rates(db_session)

    # One alert for the failed-pairs set, one for the staleness/gap check's
    # response to those same missing pairs (item 7(b)) — both real, distinct
    # failure modes per the issue's design, not a double-count of the same one.
    assert mock_alert.called
    failed_pair_alert = next(
        c for c in mock_alert.call_args_list if "FX fetch failed" in c.kwargs["subject"]
    )
    assert "USDHKD" in failed_pair_alert.kwargs["body"]
    assert "USDCNY" not in failed_pair_alert.kwargs["body"]


def test_fetch_failure_alert_is_deduped_same_day(db_session: Session, production_env: None) -> None:
    """A second run with the identical failure set on the same day must not
    re-alert — the durable Redis dedup (issue #298 pattern), not send_ops_
    alert's own Resend Idempotency-Key, is what suppresses this."""
    with (
        patch.object(fx_fetcher, "fetch_last_close", return_value={}),
        patch.object(fx_fetcher, "send_ops_alert", return_value=True) as mock_alert,
    ):
        fx_fetcher.update_fx_rates(db_session)
        fx_fetcher.update_fx_rates(db_session)

    fetch_failed_calls = [
        c for c in mock_alert.call_args_list if "FX fetch failed" in c.kwargs["subject"]
    ]
    assert len(fetch_failed_calls) == 1


def test_failed_alert_not_deduped_when_send_fails(
    db_session: Session, production_env: None
) -> None:
    """A failed send must leave the dedup state unset so the next run retries
    it (mirrors price_capture.py's _send_nav_alert round-2 review fix)."""
    with (
        patch.object(fx_fetcher, "fetch_last_close", return_value={}),
        patch.object(fx_fetcher, "send_ops_alert", return_value=False) as mock_alert,
    ):
        fx_fetcher.update_fx_rates(db_session)
        fx_fetcher.update_fx_rates(db_session)

    fetch_failed_calls = [
        c for c in mock_alert.call_args_list if "FX fetch failed" in c.kwargs["subject"]
    ]
    assert len(fetch_failed_calls) == 2


# ---------------------------------------------------------------------------
# issue #354 item 7(b): request-time resolvable-rate gap/staleness ops alert
# ---------------------------------------------------------------------------


def test_missing_pair_sends_never_resolved_alert(db_session: Session, production_env: None) -> None:
    """A pair with zero fx_rates rows ever (never fetched successfully) is a
    distinct failure from "stale" — this is the read-time gap 7(b) exists to
    catch, since a per-fetch-attempt-only alert (7a) can miss it once the
    daily task itself stops running at all."""
    points = {yf_ticker: (7.18, _AS_OF) for yf_ticker in fx_fetcher._PAIRS.values()}
    del points["USDHKD=X"]
    with (
        patch.object(fx_fetcher, "fetch_last_close", return_value=points),
        patch.object(fx_fetcher, "send_ops_alert", return_value=True) as mock_alert,
    ):
        fx_fetcher.update_fx_rates(db_session)

    missing_alert = next(
        c for c in mock_alert.call_args_list if "never resolved" in c.kwargs["subject"]
    )
    assert "USDHKD" in missing_alert.kwargs["subject"]


def test_stale_resolvable_pair_sends_stale_alert(db_session: Session, production_env: None) -> None:
    """A pair that keeps fetching "successfully" but whose resolvable latest
    rate has stopped advancing (fetch always lands on the same old
    rate_date) is the exact production mechanism this issue's root cause
    was about — 7(a) alone cannot see it since every fetch reports success."""
    old_date = date(2026, 1, 1)
    db_session.add(FxRate(pair="USDHKD", rate=Decimal("8.0"), rate_date=old_date, source="test"))
    db_session.flush()

    with (
        patch.object(fx_fetcher, "fetch_last_close") as mock_fetch,
        patch.object(fx_fetcher, "send_ops_alert", return_value=True) as mock_alert,
    ):
        # Everything except USDHKD fetches fine at the real "today"; USDHKD
        # keeps returning no new data (its yfinance point is simply absent),
        # so its only resolvable rate stays pinned at old_date.
        points = {
            yf_ticker: (7.18, datetime.now(tz=UTC))
            for yf_ticker in fx_fetcher._PAIRS.values()
            if yf_ticker != "USDHKD=X"
        }
        mock_fetch.return_value = points
        fx_fetcher.update_fx_rates(db_session)

    stale_alert = next(
        c for c in mock_alert.call_args_list if "FX pair stale" in c.kwargs["subject"]
    )
    assert "USDHKD" in stale_alert.kwargs["subject"]
    assert old_date.isoformat() in stale_alert.kwargs["body"]


def test_healthy_pairs_send_no_staleness_alert(db_session: Session, production_env: None) -> None:
    """Unlike _fake_points()'s fixed historical _AS_OF (used by the upsert-
    mechanics tests above, which don't care about staleness), the staleness
    check compares against the real current date — these points must be
    genuinely fresh (today, ET) for "no alert" to be the correct outcome."""
    fresh_as_of = datetime.now(tz=UTC)
    points = {yf_ticker: (7.18, fresh_as_of) for yf_ticker in fx_fetcher._PAIRS.values()}
    with (
        patch.object(fx_fetcher, "fetch_last_close", return_value=points),
        patch.object(fx_fetcher, "send_ops_alert", return_value=True) as mock_alert,
    ):
        fx_fetcher.update_fx_rates(db_session)

    assert mock_alert.call_count == 0
