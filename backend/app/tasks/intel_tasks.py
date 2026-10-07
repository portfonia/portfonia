"""Daily intelligence slots with one collection report per attempt."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.timezones import ET, today_et
from app.models.intel import IntelCollectionRun, IntelSlotRun
from app.services.email_sender import send_ops_alert
from app.services.headline_cleaning import EarningsCache
from app.services.instrument_news_capture import (
    collect_slot_news,
    create_instrument_run,
    error_text,
)
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_profiles import resolve_profiles
from app.services.instrument_relations import load_relations
from app.services.instrument_universe import UniverseEntry, intel_universe
from app.services.intel_deepen import DeepenRun
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_digest import batch_subject, build_batch_report
from app.services.intel_name_check import run_weekly_check, weekly_due
from app.services.intel_selection import select_units
from app.services.intel_signals import compute_signals
from app.services.news_capture import capture_news
from app.tasks import celery_app

EARLY_TRIGGER_TOLERANCE_S = 5 * 60
logger = logging.getLogger(__name__)


def now_et() -> datetime:
    return datetime.now(ET)


def weekly_check(
    session: Session, unmatched: dict[str, list[CollectedItem]], universe: list[UniverseEntry]
) -> dict[str, object]:
    """The weekly name and relation check (#681); a failure is reported, never raised."""
    try:
        return run_weekly_check(session, unmatched, universe, load_relations())
    except Exception as exc:
        logger.error("weekly name and relation check failed: %s", type(exc).__name__)
        return {"errors": [f"cleaning: {type(exc).__name__}"]}


@celery_app.task(name="app.tasks.intel_tasks.intel_slot_task")  # type: ignore[untyped-decorator]
def intel_slot_task(slot: str) -> dict[str, str]:
    if slot not in ("pre_open", "post_close"):
        raise ValueError("unknown intelligence slot")
    now = now_et()
    run_date = today_et()
    with SessionLocal() as session:
        nid = session.scalar(
            insert(IntelSlotRun)
            .values(slot=slot, run_date=run_date, started_at=now, status="running", details={})
            .on_conflict_do_nothing(constraint="uq_intel_slot_key")
            .returning(IntelSlotRun.id)
        )
        run = session.scalar(
            select(IntelSlotRun).where(IntelSlotRun.slot == slot, IntelSlotRun.run_date == run_date)
        )
        assert run is not None
        if nid is None and run.status != "failed":
            return {"status": "already_run"}
        hour, minute = (7, 30) if slot == "pre_open" else (16, 15)
        delay = (
            now - now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        ).total_seconds()
        if not -EARLY_TRIGGER_TOLERANCE_S <= delay <= 3600:
            run.started_at = now
            run.status = "failed"
            run.details = {"reason": "stale_trigger"}
            run.finished_at = now
            session.commit()
            if send_ops_alert(
                batch_subject(run, failed=True),
                f"Batch did not run: the trigger arrived too late ({timedelta(seconds=delay)} after the scheduled time).",
                idempotency_key=f"intel-report-{run.id}-{int(now.timestamp())}",
                severity="WARNING",
            ):
                run.digest_sent_at = now_et()
                session.commit()
            return {"status": "stale_trigger"}
        run.started_at = now
        run.status = "running"
        run.details = {}
        session.commit()
        earnings_cache = EarningsCache()
        deepen = None
        deepen_errors = []
        evidence: dict[str, object] = {}
        try:
            collection = create_instrument_run(session, run, now)
            universe = intel_universe(session)
            profile_errors = resolve_profiles(session, universe, now=now)
            previous = session.scalar(
                select(IntelSlotRun.started_at)
                .where(IntelSlotRun.started_at < now)
                .order_by(IntelSlotRun.started_at.desc())
                .limit(1)
            ) or now - timedelta(hours=24)
            weekend = run_date.weekday() >= 5
            priority: list[str] = []
            try:
                cfg = load_intel_deepen_config()
                signals = compute_signals(
                    session, universe, run_date, previous, cfg, slot=slot, now=now, weekend=weekend
                )
                priority = [
                    u.identifier
                    for u in select_units(signals, {}, cfg, weekend=weekend)
                    if u.kind == "mover" or u.reason.startswith("near_")
                ]
                deepen = DeepenRun(
                    session,
                    run,
                    cfg,
                    weekend,
                    now,
                    previous,
                    universe,
                    signals,
                    earnings_cache=earnings_cache,
                )
            except Exception as exc:
                deepen_errors.append(f"deepening: {type(exc).__name__}")
                logger.error(
                    "intel deepening configuration or signal step failed: %s", type(exc).__name__
                )
            pool = capture_news(session, slot_run_id=run.id, node="slot-" + slot)
            if deepen is not None:
                deepen.pool_items = pool.items
            settings = get_settings()
            budget = (
                settings.INTEL_COLLECT_BUDGET_PRE_OPEN_S
                if slot == "pre_open"
                else settings.INTEL_COLLECT_BUDGET_POST_CLOSE_S
            )
            unmatched: dict[str, list[CollectedItem]] | None = (
                {} if weekly_due(slot, run_date) else None
            )
            collection = collect_slot_news(
                session,
                run,
                now,
                budget,
                deepen.collected if deepen is not None else lambda identifier, items: None,
                collection_run=collection,
                profile_errors=profile_errors,
                priority=priority,
                earnings_cache=earnings_cache,
                unmatched=unmatched,
            )
            if deepen is not None:
                try:
                    signals = compute_signals(
                        session,
                        universe,
                        run_date,
                        previous,
                        cfg,
                        slot=slot,
                        now=now,
                        weekend=weekend,
                    )
                    deepen.finish(session, signals, pool.items)
                    evidence.update(
                        {
                            "fresh_counts": {i: s.fresh for i, s in signals.items() if s.fresh > 0},
                            "universe": [e.identifier for e in universe],
                            "theme_counts": deepen.theme_counts,
                            "theme_counts_development": deepen.theme_counts_development,
                            "deepening": deepen.details(),
                        }
                    )
                    deepen_errors.extend(deepen.errors)
                except Exception as exc:
                    deepen.close()
                    deepen_errors.append(f"deepening: {type(exc).__name__}")
            if unmatched is not None:
                evidence["weekly_check"] = weekly_check(session, unmatched, universe)
            rss = session.scalar(
                select(IntelCollectionRun)
                .where(IntelCollectionRun.slot_run_id == run.id, IntelCollectionRun.kind == "rss")
                .order_by(IntelCollectionRun.started_at.desc())
                .limit(1)
            )
            run.status = collection.status
            if profile_errors and run.status == "ok":
                run.status = "partial"
            if rss and rss.status == "partial" and run.status == "ok":
                run.status = "partial"
            if deepen_errors and run.status == "ok":
                run.status = "partial"
            evidence["deepening_errors"] = deepen_errors
            if "trigger" in run.details:
                evidence["trigger"] = run.details["trigger"]
            run.details = evidence
            if profile_errors:
                collection.errors = list(dict.fromkeys((collection.errors or []) + profile_errors))[
                    :50
                ]
            del pool
        except Exception as exc:
            if deepen is not None:
                deepen.close()
            session.rollback()
            run.status = "failed"
            run.details = {"reason": error_text("slot", exc)}
        run.finished_at = now_et()
        session.commit()
        subject, body, severity = build_batch_report(session, run)
        if send_ops_alert(
            subject,
            body,
            idempotency_key=f"intel-report-{run.id}-{int(now.timestamp())}",
            severity=severity,
        ):
            run.digest_sent_at = now_et()
            session.commit()
        return {"status": run.status}
