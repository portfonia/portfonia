"""Daily intelligence slots; collect once and send one digest per slot/date."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.timezones import ET, today_et
from app.models.intel import IntelCollectionRun, IntelSlotRun
from app.services.email_sender import send_ops_alert
from app.services.instrument_news_capture import collect_slot_news, error_text
from app.services.instrument_profiles import resolve_profiles
from app.services.instrument_universe import intel_universe
from app.services.intel_digest import build_slot_digest
from app.services.news_capture import capture_news
from app.tasks import celery_app


def now_et() -> datetime:
    return datetime.now(ET)


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
        if (
            now - now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        ).total_seconds() > 3600:
            run.status = "failed"
            run.details = {"reason": "stale_trigger"}
            run.finished_at = now
            session.commit()
            return {"status": "stale_trigger"}
        run.started_at = now
        run.status = "running"
        run.details = {}
        session.commit()
        try:
            profile_errors = resolve_profiles(session, intel_universe(session), now=now)
            pool = capture_news(session, slot_run_id=run.id, node="slot-" + slot)
            settings = get_settings()
            budget = (
                settings.INTEL_COLLECT_BUDGET_PRE_OPEN_S
                if slot == "pre_open"
                else settings.INTEL_COLLECT_BUDGET_POST_CLOSE_S
            )
            collection = collect_slot_news(
                session, run, now, budget, lambda identifier, items: None
            )
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
            if profile_errors:
                collection.errors = list(dict.fromkeys((collection.errors or []) + profile_errors))[
                    :50
                ]
            del pool
        except Exception as exc:
            session.rollback()
            run.status = "failed"
            run.details = {"reason": error_text("slot", exc)}
        run.finished_at = now_et()
        session.commit()
        subject, body, severity = build_slot_digest(session, run)
        if send_ops_alert(
            subject, body, idempotency_key=f"intel-digest-{run_date}-{slot}", severity=severity
        ):
            run.digest_sent_at = now_et()
            session.commit()
        return {"status": run.status}
