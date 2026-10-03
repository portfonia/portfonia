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
from app.services.intel_digest import build_batch_report
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
    assert send.call_count == 1 and send.call_args.kwargs["severity"] == "WARNING"


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
                slot_run_id=slot.id,
                node=node,
                started_at=NOW,
                finished_at=NOW,
                status="ok",
                stats={"feeds": {"FT": {"items": 2, "errors": 0}}},
                errors=[],
            )
        )
    db_session.flush()
    _, body, severity = build_batch_report(db_session, slot)
    assert (
        "Instruments covered: 1 of 3" in body
        and "INTC,AMKR" in body
        and "RSS feeds (2) ...... 4, no errors" in body
    )
    assert body.index("PART 1 - FREE NEWS COLLECTION") < body.index("PART 2 - PAID DEEPENING")
    assert "ET" in build_batch_report(db_session, slot)[0] and severity == "INFO"
    assert (
        "Not an article page ...................... 5" in body
        and "four" not in body
        and "0.002" in body
    )
    slot.status = "failed"
    assert build_batch_report(db_session, slot)[2] == "WARNING"


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


@pytest.mark.parametrize("slot,hour,minute", [("post_close", 0, 40), ("pre_open", 3, 0)])
def test_midnight_stale_does_not_block_real_slot(
    db_session: Session, slot: str, hour: int, minute: int
) -> None:
    early = NOW.replace(hour=hour, minute=minute)
    real = NOW.replace(hour=7, minute=30) if slot == "pre_open" else NOW
    with (
        patch.object(task, "SessionLocal", return_value=db_session),
        patch.object(task, "today_et", return_value=NOW.date()),
        patch.object(task, "now_et", return_value=early) as clock,
        patch.object(task, "resolve_profiles", return_value=[]),
        patch.object(task, "capture_news", return_value=PoolCaptureResult(0, [])) as pool,
        patch.object(
            task, "collect_slot_news", return_value=IntelCollectionRun(status="ok")
        ) as collect,
        patch.object(task, "send_ops_alert", return_value=True) as send,
    ):
        assert task.intel_slot_task(slot) == {"status": "stale_trigger"}
        row = db_session.scalars(select(IntelSlotRun)).one()
        assert row.status == "failed" and row.details == {"reason": "stale_trigger"}
        pool.assert_not_called()
        assert send.call_count == 1
        clock.return_value = real
        assert task.intel_slot_task(slot) == {"status": "ok"}
        assert pool.call_count == collect.call_count == 1
        assert send.call_count == 2
    assert len(db_session.scalars(select(IntelSlotRun)).all()) == 1


@pytest.mark.parametrize(
    "offset,expected",
    [
        (-301, "stale_trigger"),
        (-300, "ok"),
        (0, "ok"),
        (3300, "ok"),
        (3600, "ok"),
        (3601, "stale_trigger"),
    ],
)
def test_slot_trigger_tolerance_boundaries(db_session: Session, offset: int, expected: str) -> None:
    with (
        patch.object(task, "SessionLocal", return_value=db_session),
        patch.object(task, "today_et", return_value=NOW.date()),
        patch.object(task, "now_et", return_value=NOW + timedelta(seconds=offset)),
        patch.object(task, "resolve_profiles", return_value=[]),
        patch.object(task, "capture_news", return_value=PoolCaptureResult(0, [])),
        patch.object(task, "collect_slot_news", return_value=IntelCollectionRun(status="ok")),
        patch.object(task, "send_ops_alert", return_value=True),
    ):
        assert task.intel_slot_task("post_close") == {"status": expected}


def test_failed_name_lookup_once_per_slot_after_run_creation(db_session: Session) -> None:
    from app.services import instrument_news_capture as cap
    from app.services import instrument_profiles as profiles
    from app.services.instrument_universe import UniverseEntry

    entry = UniverseEntry("UNRESOLVED", "UNRESOLVED", "UK")
    seen: list[int] = []

    def lookup(*args: object) -> None:
        seen.append(
            len(
                db_session.scalars(
                    select(IntelCollectionRun).where(
                        IntelCollectionRun.kind == "instrument",
                        IntelCollectionRun.status == "running",
                    )
                ).all()
            )
        )
        raise ValueError("name lookup failed")

    def factory() -> Session:
        return Session(bind=db_session.connection(), join_transaction_mode="create_savepoint")

    with (
        patch.object(task, "SessionLocal", return_value=db_session),
        patch.object(task, "now_et", return_value=NOW),
        patch.object(task, "today_et", return_value=NOW.date()),
        patch.object(task, "intel_universe", return_value=[entry]),
        patch.object(cap, "intel_universe", return_value=[entry]),
        patch.object(profiles, "load_entity_aliases", return_value={}),
        patch("app.services.instrument_profiles.yf.Ticker", side_effect=lookup) as query,
        patch.object(cap, "sources_for", return_value=[]),
        patch.object(cap, "SessionLocal", side_effect=factory),
        patch.object(task, "capture_news", return_value=PoolCaptureResult(0, [])),
        patch.object(task, "send_ops_alert", return_value=True),
    ):
        assert task.intel_slot_task("post_close") == {"status": "partial"}
    assert query.call_count == 1
    assert seen == [1]
    collection = db_session.scalars(select(IntelCollectionRun)).one()
    assert collection.instruments_total == collection.instruments_processed == 1
    assert collection.errors == ["profile: ValueError"]
