"""Fetch live FX quotes (yfinance, with a Twelve Data per-pair fallback)
and upsert into fx_rates table, once per each of the two daily capture
attempts (issue #519) — not a daily-close/daily-bar fetch; see
`capture_fx_rates`'s docstring for why."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

import yfinance as yf
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.alert_dedup import already_alerted, mark_alerted
from app.core.config import get_settings
from app.core.timezones import ET
from app.models.fx_rate import FxRate
from app.services._twelvedata import fetch_live_rate as twelvedata_fetch_live_rate
from app.services._yfinance import _quiet_yfinance_logs, fetch_live_rate
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


def _send_fx_alert(
    subject: str,
    body: str,
    dedup_key: str,
    severity: Literal["INFO", "WARNING", "ALERT"],
) -> None:
    """Send an FX ops alert unless this dedup_key was already alerted.

    Mirrors price_capture.py's _send_nav_alert exactly (issue #298 precedent
    named in issue #354's constraints): the durable Redis dedup, not the
    Resend Idempotency-Key, is what stops a daily-beat re-alert on a
    persisting condition; the dedup key is recorded only after confirmed
    delivery so a failed send leaves the state un-deduped for the next beat.

    Gated on APP_ENV == "production" (same field/convention db_backup.py
    already uses) — issue #354 follow-up: a local dev Postgres is never
    kept fresh (no Celery beat runs there), so its fx_rates table reads as
    permanently stale/missing by design, not by incident. Without this gate,
    anyone manually exercising update_fx_rates()/capture_fx_task() against
    local dev with a real RESEND_API_KEY configured sends real alerts to the
    admin inbox for an expected dev-only condition. logger.warning/error
    calls at the call sites are unaffected — only the real send is skipped.
    """
    if get_settings().APP_ENV != "production":
        return
    if already_alerted(dedup_key):
        return
    if send_ops_alert(subject=subject, body=body, idempotency_key=dedup_key, severity=severity):
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
        severity="ALERT",
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
                severity="ALERT",
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
                severity="WARNING",
            )


def _fetch_rates(pairs: dict[str, str]) -> dict[str, tuple[Decimal, date]]:
    """
    Batch-fetch live quotes for the given yfinance FX tickers (issue #519 —
    was a daily-close fetch; a `=X` FX ticker trades 24/5 and has no real
    daily close to wait for).

    Returns {pair_name: (rate, rate_date_et)} where rate_date_et is the
    ET calendar date at fetch time, not a trading-day/bar date. Pairs with
    no data are omitted.
    """
    points = fetch_live_rate(list(pairs.values()))

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


def _fetch_rate_history(
    pairs: dict[str, str], period: str
) -> dict[str, list[tuple[date, Decimal]]]:
    """{pair_name: [(rate_date, rate), ...]} oldest -> newest.

    `period` is passed straight to `yf.download` — the multi-year seed uses
    `Ny` (issue #398 / same lesson as `backfill_benchmark_prices`, review
    5124107298 finding 2). Callers must not hand this a huge `Nd` window.
    """
    yf_tickers = list(pairs.values())
    if not yf_tickers:
        return {}
    try:
        with _quiet_yfinance_logs():
            hist = yf.download(
                tickers=" ".join(yf_tickers), period=period, auto_adjust=True, progress=False
            )
    except Exception:
        logger.exception("fx_fetcher: yfinance history download failed for %s", yf_tickers)
        return {}
    if hist.empty:
        return {}

    close = hist["Close"]
    yf_to_pair = {yf_ticker: pair_name for pair_name, yf_ticker in pairs.items()}
    out: dict[str, list[tuple[date, Decimal]]] = {}
    for yf_ticker in yf_tickers:
        try:
            series = close[yf_ticker] if len(yf_tickers) > 1 else close
        except KeyError:
            continue
        rows: list[tuple[date, Decimal]] = []
        for ts, value in series.items():
            if value != value:  # NaN
                continue
            rows.append((ts.date(), Decimal(str(float(value)))))
        if rows:
            out[yf_to_pair[yf_ticker]] = rows
    return out


# PostgreSQL's hard limit is 65,535 bound parameters per statement. At 5
# columns/row, 5,000 rows/batch = 25,000 params -- comfortable headroom
# even as `_PAIRS` or the backfill window grows (issue #402: a single
# unbatched INSERT over 5 years x 14 pairs produced ~87,850 params and
# raised `psycopg.OperationalError` before any row was written).
_UPSERT_BATCH_SIZE = 5000


def _upsert_fx_history(session: Session, rows: list[dict[str, object]]) -> int:
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), _UPSERT_BATCH_SIZE):
        chunk = rows[start : start + _UPSERT_BATCH_SIZE]
        base = insert(FxRate).values(chunk)
        stmt = base.on_conflict_do_update(
            constraint="uq_fx_rates_pair_rate_date",
            set_={"rate": base.excluded.rate, "fetched_at": base.excluded.fetched_at},
        ).returning(FxRate.id)
        written += len(session.execute(stmt).fetchall())
    return written


def backfill_fx_rates(session: Session, years: int = 5) -> int:
    """One-off ~`years` daily-close seed for every `_PAIRS` entry.

    Observed FX, not portfolio replay — no `is_backfilled`. Safe to re-run
    (idempotent upsert on `(pair, rate_date)`). Does not loosen the 10-day
    as-of bound and does not invent rates on gaps. Daily `update_fx_rates`
    remains the freshness path.
    """
    fetched_at = datetime.now(tz=UTC)
    fetched = _fetch_rate_history(_PAIRS, period=f"{max(years, 1)}y")
    rows: list[dict[str, object]] = []
    for pair_name, points in fetched.items():
        for rate_date, rate in points:
            rows.append(
                {
                    "pair": pair_name,
                    "rate": rate,
                    "rate_date": rate_date,
                    "source": "yfinance",
                    "fetched_at": fetched_at,
                }
            )
    written = _upsert_fx_history(session, rows)
    missing = [pair_name for pair_name in _PAIRS if pair_name not in fetched]
    if missing:
        logger.warning("backfill_fx_rates: no history for %s", ", ".join(sorted(missing)))
    print(
        f"[OK] backfilled {written} fx_rates row(s) across {len(fetched)} pair(s)"
        + (f" (missing: {', '.join(sorted(missing))})" if missing else "")
    )
    return written


@dataclass
class FxCaptureResult:
    recovered_via_yfinance: list[str] = field(default_factory=list)
    recovered_via_fallback: list[str] = field(default_factory=list)
    still_missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "recovered_via_yfinance": self.recovered_via_yfinance,
            "recovered_via_fallback": self.recovered_via_fallback,
            "still_missing": self.still_missing,
        }


def _twelvedata_key() -> str | None:
    key = get_settings().TWELVEDATA_API_KEY
    return key.get_secret_value() if key else None


def _twelvedata_symbol(pair: str) -> str:
    """DB pair name to Twelve Data symbol, e.g. USDCNY -> USD/CNY: every
    `_PAIRS` key is USD + a 3-letter code."""
    return f"{pair[:3]}/{pair[3:]}"


# Twelve Data free tier: 8 requests/minute (`_twelvedata.py`'s own comment).
# A fixed inter-call delay keeps a many-pairs-missing run under that cap
# instead of bursting through it (issue #518's root cause: 8 calls 400'd,
# the next 6 429'd because nothing paced them).
_TWELVEDATA_MIN_INTERVAL_SECONDS = 8.0


def capture_fx_rates(session: Session) -> FxCaptureResult:
    """Fetch today's live FX rate for every pair (issue #519).

    Replaces #426's daily-bar catch-up: yfinance's `fetch_live_rate` either
    returns today's quote right now or it doesn't (there is no bar to wait
    for), so any pair it misses falls back to Twelve Data's live quote
    immediately, in the same run — not on a later retry against a
    specific missed calendar day.

    Called symmetrically by both scheduled attempts (16:00 ET day-session
    close, 20:00 ET evening-session close, `app/tasks/__init__.py`) —
    neither is a "catch-up" of the other; each is a fresh attempt.
    """
    result = FxCaptureResult()
    fetch_result = update_fx_rates(session)
    session.flush()
    missing = set(fetch_result.failed)
    result.recovered_via_yfinance = sorted(set(_PAIRS) - missing)

    if missing:
        api_key = _twelvedata_key()
        if api_key is None:
            logger.warning(
                "capture_fx_rates: %d pair(s) missing from yfinance and "
                "TWELVEDATA_API_KEY is unset, no fallback available: %s",
                len(missing),
                sorted(missing),
            )
        else:
            today = datetime.now(tz=ET).date()
            fetched_at = datetime.now(tz=UTC)
            for i, pair_name in enumerate(sorted(missing)):
                if i > 0:
                    time.sleep(_TWELVEDATA_MIN_INTERVAL_SECONDS)
                try:
                    rate = twelvedata_fetch_live_rate(_twelvedata_symbol(pair_name), api_key)
                except Exception:
                    logger.exception(
                        "capture_fx_rates: twelvedata fallback failed for %s", pair_name
                    )
                    continue
                stmt = (
                    insert(FxRate)
                    .values(
                        pair=pair_name,
                        rate=rate,
                        rate_date=today,
                        source="twelvedata",
                        fetched_at=fetched_at,
                    )
                    .on_conflict_do_update(
                        constraint="uq_fx_rates_pair_rate_date",
                        set_={"rate": rate, "fetched_at": fetched_at, "source": "twelvedata"},
                    )
                )
                session.execute(stmt)
                session.flush()
                missing.discard(pair_name)
                result.recovered_via_fallback.append(pair_name)

    result.still_missing = sorted(missing)
    if result.still_missing:
        today_str = datetime.now(tz=ET).date().isoformat()
        _send_fx_alert(
            subject=f"[Portfonia] FX capture still missing {len(result.still_missing)} "
            f"pair(s) — {today_str}",
            body=(
                f"After a live-quote fetch and a Twelve Data fallback attempt, "
                f"{today_str}'s rate is still missing for: "
                + ", ".join(result.still_missing)
                + ".\n\nHoldings/base-currency conversions in these currencies will keep "
                "using an older rate until a fresher one is captured.\n\n"
                "Check worker.log for capture_fx_task/capture_fx_evening_task."
            ),
            dedup_key=(
                f"ops-fx-capture-still-missing-{today_str}-" + "-".join(result.still_missing)
            ),
            severity="WARNING",
        )
    return result
