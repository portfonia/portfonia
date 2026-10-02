"""Issue #620 slot, digest and first-run acceptance."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.holding import Holding
from app.models.intel import InstrumentProfile, IntelCollectionRun, IntelSlotRun, NewsInstrument
from app.models.news import News
from app.services.intel_digest import build_slot_digest
from app.services.news_capture import PoolCaptureResult
from app.services.news_fetcher import FetchNewsResult
from app.tasks import celery_app
from app.tasks import intel_tasks as task
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_intel_records import NOW, item


def run(session: Session, status: str = "partial") -> IntelSlotRun:
    slot = IntelSlotRun(
        slot="post_close",
        run_date=NOW.date(),
        started_at=NOW,
        status=status,
        finished_at=NOW + timedelta(minutes=1),
        details={},
    )
    session.add(slot)
    session.flush()
    return slot


def test_acceptance_11_slot_idempotency(db_session: Session) -> None:
    with (
        patch.object(task, "SessionLocal", return_value=db_session),
        patch.object(task, "now_et", return_value=NOW),
        patch.object(task, "today_et", return_value=NOW.date()),
        patch.object(task, "resolve_profiles", return_value=[]),
        patch.object(task, "capture_news", return_value=PoolCaptureResult(0, [])),
        patch.object(task, "collect_slot_news", return_value=IntelCollectionRun(status="ok")),
        patch.object(task, "send_ops_alert", return_value=True) as send,
    ):
        task.intel_slot_task("post_close")
        task.intel_slot_task("post_close")
    assert len(db_session.scalars(select(IntelSlotRun)).all()) == 1 and send.call_count == 1


def test_acceptance_12_stale_trigger(db_session: Session) -> None:
    with (
        patch.object(task, "SessionLocal", return_value=db_session),
        patch.object(task, "now_et", return_value=NOW.replace(hour=18, minute=30)),
        patch.object(task, "capture_news") as pool,
        patch.object(task, "send_ops_alert") as send,
    ):
        task.intel_slot_task("post_close")
    slot = db_session.scalars(select(IntelSlotRun)).one()
    assert slot.status == "failed" and slot.details["reason"] == "stale_trigger"
    pool.assert_not_called()
    send.assert_not_called()


def test_acceptance_13_24_digest(db_session: Session) -> None:
    slot = run(db_session)
    db_session.add(
        IntelCollectionRun(
            kind="instrument",
            slot_run_id=slot.id,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=10),
            status="partial",
            stats={
                "markets": {"US": {"total": 3, "processed": 1}},
                "unreached": {"US": ["INTC", "AMKR"]},
                "cleaning": {"non_article": 5},
                "cleaning_samples": {"non_article": ["one", "two", "three", "four"]},
                "classifier": {"batches": 2, "failed_batches": 1, "cost_usd": 0.002},
            },
            errors=[],
        )
    )
    for node in ["US-close", "Japan-close"]:
        db_session.add(
            IntelCollectionRun(
                kind="rss",
                node=node,
                started_at=NOW - timedelta(hours=1),
                finished_at=NOW,
                status="ok",
                stats={"feeds": {"FT": {"items": 2, "errors": 0}}},
                errors=[],
            )
        )
    db_session.flush()
    _, body, severity = build_slot_digest(db_session, slot)
    assert "33.3%" in body and "INTC,AMKR" in body and "US-close" in body and "Japan-close" in body
    assert body.index("Instrument collection:") < body.index("RSS node US-close")
    assert "ET" in body and severity == "INFO"
    assert "non_article: 5" in body and "four" not in body and "0.002" in body
    slot.status = "failed"
    assert build_slot_digest(db_session, slot)[2] == "WARNING"


def test_acceptance_14_daily_schedule() -> None:
    for slot, hour, minute in [("pre_open", 7, 30), ("post_close", 16, 15)]:
        row = celery_app.conf.beat_schedule["intel-slot-" + slot]
        assert row["schedule"].hour == {hour} and row["schedule"].minute == {minute}
        assert len(row["schedule"].day_of_week) == 7
    assert len([k for k in celery_app.conf.beat_schedule if k.startswith("capture-news-")]) == 16


def test_acceptance_33_first_run_pool_link(db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)
    db_session.add(
        Holding(
            user_id=TEST_USER_ID,
            name="private",
            ticker="NVDA",
            market="US",
            asset_type="stock",
            pricing_mode="auto",
            currency="USD",
        )
    )
    db_session.flush()
    with (
        patch.object(task, "SessionLocal", return_value=db_session),
        patch.object(task, "now_et", return_value=NOW),
        patch.object(task, "today_et", return_value=NOW.date()),
        patch("app.services.news_capture.fetch_news", return_value=FetchNewsResult([item()], [])),
        patch.object(task, "collect_slot_news", return_value=IntelCollectionRun(status="ok")),
        patch.object(task, "send_ops_alert", return_value=True),
    ):
        task.intel_slot_task("post_close")
    assert db_session.scalars(select(NewsInstrument)).one().identifier == "NVDA"
    assert db_session.scalars(select(InstrumentProfile)).one().name_en == "Nvidia"


def test_acceptance_26_full_slot_has_no_persisted_urls(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from app.services import instrument_news_capture as cap
    from app.services.instrument_news_sources import CollectedItem

    seed_user(db_session, TEST_USER_ID)
    db_session.add(
        Holding(
            user_id=TEST_USER_ID,
            name="private",
            ticker="NVDA",
            market="US",
            asset_type="stock",
            pricing_mode="auto",
            currency="USD",
        )
    )
    db_session.flush()
    settings = get_settings().model_copy(update={"INTEL_COLLECT_WORKERS": 1})

    def factory() -> Session:
        return Session(bind=db_session.connection(), join_transaction_mode="create_savepoint")

    with (
        patch.object(task, "SessionLocal", return_value=db_session),
        patch.object(task, "now_et", return_value=NOW),
        patch.object(task, "today_et", return_value=NOW.date()),
        patch("app.services.news_capture.fetch_news", return_value=FetchNewsResult([item()], [])),
        patch.object(cap, "SessionLocal", side_effect=factory),
        patch.object(cap, "get_settings", return_value=settings),
        patch.object(
            cap,
            "sources_for",
            return_value=[
                (
                    "yahoo",
                    lambda: [
                        CollectedItem(
                            "Nvidia launches a new chip",
                            NOW,
                            "https://fixturepublisher.example/new",
                        )
                    ],
                )
            ],
        ),
        patch.object(cap, "classify_headlines", return_value=({0: "keep"}, 0, None)),
        patch.object(task, "send_ops_alert", return_value=True) as send,
        caplog.at_level(logging.INFO),
    ):
        task.intel_slot_task("post_close")
    for model in [News, NewsInstrument, InstrumentProfile, IntelCollectionRun, IntelSlotRun]:
        for row in db_session.scalars(select(model)):
            for column in model.__table__.columns:
                value = getattr(row, column.name)
                if isinstance(value, (str, dict, list)):
                    assert "http://" not in str(value) and "https://" not in str(value)
                    if model in [News, NewsInstrument, InstrumentProfile]:
                        assert "FixturePublisher" not in str(
                            value
                        ) and "fixturepublisher.example" not in str(value)
    assert "https://" not in caplog.text and "http://" not in caplog.text
    assert "https://" not in send.call_args.args[1]
