"""Compute shared day facts in the weekday post-close intelligence slot."""

from dataclasses import asdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.intel import IntelSlotRun, NewsInstrument
from app.models.macro_event_intel import MacroEventIntel
from app.models.news import News
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.ticker_intel import TickerIntel
from app.models.user import User
from app.services.cross_name_intel import get_day_synthesis
from app.services.instrument_universe import UniverseEntry
from app.services.intel_selection import WorkUnit
from app.services.macro_detector import detect_macro_signals
from app.services.macro_event_intel import (
    build_l2_facts,
    get_l2_intel_batch,
    l2_event_keys_for_user,
)
from app.services.report_serializers import _serialize_macro
from app.services.technical_position import compute_technical_positions
from app.services.ticker_intel import build_l1_facts, get_l1_intel_batch, large_weight_identifiers
from app.services.window_data import (
    day_window_bounds,
    load_day_news,
    lookback_trading_dates,
    resolve_global_moves,
)


def l1_candidates(
    session: Session, universe: list[UniverseEntry], selected: list[WorkUnit]
) -> list[str]:
    allowed = {e.identifier for e in universe}
    candidates = [u.identifier for u in selected if u.identifier]
    for user_id in session.scalars(select(User.id).where(User.status == "active")):
        rows = list(
            session.scalars(
                select(PortfolioValueSnapshot)
                .where(PortfolioValueSnapshot.user_id == user_id)
                .order_by(PortfolioValueSnapshot.snapshot_date.desc())
            )
        )
        if not rows:
            continue
        latest = rows[0].snapshot_date
        holdings = [
            {"ticker": r.ticker, "fund_code": r.fund_code, "market_value_base": r.market_value_base}
            for r in rows
            if r.snapshot_date == latest
        ]
        candidates.extend(large_weight_identifiers(holdings))
    return list(dict.fromkeys(i for i in candidates if i in allowed))


def linked_headlines(
    session: Session, identifiers: list[str], dates: list[date]
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    if not identifiers or not dates:
        return result
    start, _ = day_window_bounds(dates[0])
    _, end = day_window_bounds(dates[-1])
    for identifier, news in session.execute(
        select(NewsInstrument.identifier, News)
        .join(News, News.id == NewsInstrument.news_id)
        .where(
            NewsInstrument.identifier.in_(identifiers),
            News.published_at >= start,
            News.published_at <= end,
        )
        .order_by(News.published_at.desc())
    ):
        day = news.published_at.astimezone(ET).date()
        titles = result.setdefault(identifier, [])
        if day in dates and len(titles) < 8:
            titles.append(f"{day}: {news.record['title']}")
    return result


def run_post_close_analysis(
    session: Session,
    slot_run: IntelSlotRun,
    selected: list[WorkUnit],
    universe: list[UniverseEntry],
) -> dict[str, object]:
    day = slot_run.run_date
    result: dict[str, object] = {"errors": []}
    errors: list[str] = []
    try:
        candidates = l1_candidates(session, universe, selected)
        dates = lookback_trading_dates(day)
        moves = {d: resolve_global_moves(session, *day_window_bounds(d))[0] for d in dates}
        technical = [
            asdict(t)
            for t in compute_technical_positions(
                session, [{"ticker": i, "name": i} for i in candidates], day
            )
        ]
        facts = build_l1_facts(
            candidates,
            moves.get(day, {}),
            linked_headlines(session, candidates, dates),
            technical,
            moves,
        )
        before = set(
            session.scalars(
                select(TickerIntel.identifier).where(
                    TickerIntel.trade_date == day, TickerIntel.analysis.is_not(None)
                )
            )
        )
        intel = get_l1_intel_batch(session, candidates, day, facts, users_remaining=1)
        session.commit()
        result.update(
            {
                "l1_candidates": len(candidates),
                "l1_written": len(set(intel) - before),
                "l1_cache_hits": len(set(intel) & before),
            }
        )
    except Exception as exc:
        session.rollback()
        errors.append(f"L1: {type(exc).__name__}")
    try:
        keys = l2_event_keys_for_user(
            session, day, _serialize_macro(detect_macro_signals(load_day_news(session, day)))
        )
        before = set(
            session.scalars(
                select(MacroEventIntel.event_key).where(
                    MacroEventIntel.trade_date == day, MacroEventIntel.analysis.is_not(None)
                )
            )
        )
        intel2 = get_l2_intel_batch(
            session, keys, day, build_l2_facts(session, keys, day), users_remaining=1
        )
        session.commit()
        result.update({"l2_keys": len(keys), "l2_written": len(set(intel2) - before)})
    except Exception as exc:
        session.rollback()
        errors.append(f"L2: {type(exc).__name__}")
    try:
        clusters = get_day_synthesis(session, day)
        session.commit()
        result["l3_clusters"] = len(clusters)
    except Exception as exc:
        session.rollback()
        errors.append(f"L3: {type(exc).__name__}")
    result["errors"] = errors
    return result
