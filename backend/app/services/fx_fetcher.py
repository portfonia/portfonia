"""Fetch daily FX rates from yfinance and upsert into fx_rates table."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.alert_dedup import already_alerted, mark_alerted
from app.core.timezones import ET
from app.models.fx_rate import FxRate
from app.services._yfinance import fetch_last_close
from app.services.email_sender import send_ops_alert

logger = logging.getLogger(__name__)

# Pairs to fetch: DB name → yfinance ticker. Must cover every
# VALID_CURRENCIES entry other than USD (issue #204: GBP and 10 other valid
# currencies had no pair here, so fx_rates never had a rate for them and
# portfolio_calculator's _to_base always returned None for those holdings).
_PAIRS: dict[str, str] = {
    "USDCNY": "USDCNY=X",
    "USDHKD": "USDHKD=X",
    "USDCNH": "USDCNH=X",
    "USDGBP": "USDGBP=X",
    "USDEUR": "USDEUR=X",
    "USDJPY": "USDJPY=X",
    "USDSGD": "USDSGD=X",
    "USDAUD": "USDAUD=X",
    "USDCAD": "USDCAD=X",
    "USDCHF": "USDCHF=X",
    "USDKRW": "USDKRW=X",
    "USDTWD": "USDTWD=X",
    "USDMOP": "USDMOP=X",
    "USDNZD": "USDNZD=X",
}


@dataclass
class FxFetchResult:
    upserted: int = 0
    failed: list[str] = field(default_factory=list)


# Calendar days beyond which a pair's latest resolvable rate is considered
# stale — mirrors portfolio_calculator._PRICE_STALE_DAYS / report_sections.
# _FX_STALE_DAYS (issue #299/#354): one mental model for "how stale is too
# stale" across capture, request-time conversion, and report text.
_FX_STALE_DAYS = 4
# Dedup keys embed the failing state (pair + date, or pair + stale rate_date),
# so this TTL is a garbage-collection safety net only — same convention as
# price_capture.py's fund-NAV alert dedup (issue #298).
_ALERT_DEDUP_TTL_SECONDS = 90 * 24 * 60 * 60


def _send_fx_alert(subject: str, body: str, dedup_key: str) -> None:
    """Send an FX ops alert unless this dedup_key was already alerted.

    Mirrors price_capture.py's _send_nav_alert exactly (issue #298 precedent
    named in issue #354's constraints): the durable Redis dedup, not the
    Resend Idempotency-Key, is what stops a daily-beat re-alert on a
    persisting condition; the dedup key is recorded only after confirmed
    delivery so a failed send leaves the state un-deduped for the next beat.
    """
    if already_alerted(dedup_key):
        return
    if send_ops_alert(subject=subject, body=body, idempotency_key=dedup_key):
        mark_alerted(dedup_key, _ALERT_DEDUP_TTL_SECONDS)


def _warn_failed_pairs(failed: list[str], today: date) -> None:
    """Issue #354 item 7(a): update_fx_rates() previously only logged a per-
    pair fetch miss (or a total-fetch failure) — nothing ever reached the
    ops inbox, so a persistently-failing pair (or a total yfinance outage)
    was invisible outside worker.log. Keyed per (sorted failed-pair-set,
    today) so a stable failure set alerts once per day, and a change in
    which pairs are failing produces a fresh alert.
    """
    if not failed:
        return
    _send_fx_alert(
        subject=f"[Portfonia] FX fetch failed — {len(failed)} pair(s)",
        body=(
            f"update_fx_rates got no data on {today.isoformat()} for: "
            + ", ".join(sorted(failed))
            + "\n\nHoldings/base-currency conversions in the affected currencies "
            "may fail or use a stale rate until the next successful fetch.\n\n"
            "Check worker.log for yfinance errors on these pairs."
        ),
        dedup_key=f"ops-fx-fetch-failed-{'-'.join(sorted(failed))}-{today.isoformat()}",
    )


def _check_fx_staleness(session: Session, today: date) -> None:
    """Issue #354 item 7(b): a genuine gap in what a request-time
    _load_fx_rates() can resolve, distinct from (a) — a pair can fetch
    successfully every day forever and still be the class of failure this
    issue's root cause was about (its *resolvable* latest rate trailing
    other pairs, or never having a row at all). Checked once per daily
    capture run (not per request) against the full expected `_PAIRS` set,
    mirroring price_capture.py's _warn_if_nav_missing/_warn_if_nav_stale
    pattern. Missing entirely and merely-stale are reported as separate
    alerts (different remediation: "never captured" vs. "capture stalled").
    """
    rows = session.execute(
        select(FxRate.pair, func.max(FxRate.rate_date))
        .where(FxRate.rate_date <= today)
        .group_by(FxRate.pair)
    ).all()
    latest_by_pair: dict[str, date] = {pair: latest for pair, latest in rows}

    for pair_name in _PAIRS:
        latest = latest_by_pair.get(pair_name)
        if latest is None:
            _send_fx_alert(
                subject=f"[Portfonia] FX pair never resolved — {pair_name}",
                body=(
                    f"{pair_name} has no fx_rates row on or before {today.isoformat()}. "
                    f"Holdings/base-currency conversions needing this pair will fail "
                    f"outright (rendered as unpriced) rather than use a stale rate.\n\n"
                    f"Check worker.log for capture_fx_task and fx_rates for this pair."
                ),
                dedup_key=f"ops-fx-pair-missing-{pair_name}-{today.isoformat()}",
            )
            continue
        lag = (today - latest).days
        if lag > _FX_STALE_DAYS:
            _send_fx_alert(
                subject=f"[Portfonia] FX pair stale — {pair_name}",
                body=(
                    f"{pair_name}'s latest resolvable rate is dated {latest.isoformat()}, "
                    f"{lag} calendar day(s) behind {today.isoformat()}.\n\n"
                    f"Holdings/base-currency conversions using this pair will use a "
                    f"stale exchange rate until a fresher rate is fetched.\n\n"
                    f"Check worker.log for capture_fx_task runs and fx_rates for this pair."
                ),
                dedup_key=f"ops-fx-pair-stale-{pair_name}-{latest.isoformat()}",
            )


def _fetch_rates(pairs: dict[str, str]) -> dict[str, tuple[Decimal, date]]:
    """
    Batch-fetch close rates for the given yfinance FX tickers.

    Returns {pair_name: (rate, rate_date_et)} where rate_date_et is the
    trading-day date in US Eastern Time (design §6.2). Pairs with no data
    are omitted.
    """
    points = fetch_last_close(list(pairs.values()))

    result: dict[str, tuple[Decimal, date]] = {}
    for pair_name, yf_ticker in pairs.items():
        point = points.get(yf_ticker)
        if point is None:
            continue
        rate_value, as_of = point
        rate_date = as_of.astimezone(ET).date()
        result[pair_name] = (Decimal(str(rate_value)), rate_date)

    return result


def update_fx_rates(session: Session) -> FxFetchResult:
    """
    Fetch today's FX rates and upsert into fx_rates table.

    Uses INSERT ... ON CONFLICT DO UPDATE so re-running is safe.
    fetched_at is always updated on conflict to reflect the latest fetch time.
    """
    result = FxFetchResult()
    fetched_at = datetime.now(tz=UTC)
    today_et = fetched_at.astimezone(ET).date()

    rates = _fetch_rates(_PAIRS)
    if not rates:
        result.failed = list(_PAIRS.keys())
        logger.error("yfinance returned no FX data")
        _warn_failed_pairs(result.failed, today_et)
        _check_fx_staleness(session, today_et)
        return result

    for pair_name, (rate, rate_date) in rates.items():
        stmt = (
            insert(FxRate)
            .values(
                pair=pair_name,
                rate=rate,
                rate_date=rate_date,
                source="yfinance",
                fetched_at=fetched_at,
            )
            .on_conflict_do_update(
                constraint="uq_fx_rates_pair_rate_date",
                set_={"rate": rate, "fetched_at": fetched_at},
            )
        )
        session.execute(stmt)
        result.upserted += 1
        logger.info("FX %s = %.6f  rate_date=%s", pair_name, rate, rate_date)

    for pair_name in _PAIRS:
        if pair_name not in rates:
            result.failed.append(pair_name)
            logger.warning("no data for FX pair %s", pair_name)

    session.flush()
    _warn_failed_pairs(result.failed, today_et)
    _check_fx_staleness(session, today_et)
    return result
