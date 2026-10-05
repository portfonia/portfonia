"""Merged weekday scheduling acceptance for issue #650."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.models.operational_event import OperationalEvent
from app.models.report import Report
from app.models.user import User
from app.services import subscription as s
from app.tasks import celery_app, next_occurrence_for_cadence
from app.tasks import report_tasks as task
from app.tests.test_subscription import funded_user, subscription_rows


def freeze(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            return cls.fromtimestamp(now.timestamp(), tz=tz)

    monkeypatch.setattr(task, "datetime", Clock)
    monkeypatch.setattr(task, "today_et", lambda: now.astimezone(ET).date())


def subscriber(session: Session, cadence: str, status: str = "active") -> User:
    user = funded_user(session)
    user.subscription_status = status
    user.subscription_type = user.report_cadence = cadence
    user.subscription_period_start = date(2026, 9, 1)
    user.subscription_expires_on = date(2026, 9, 30) if status == "expired" else date(2026, 11, 1)
    user.subscription_anchor_day = 1
    session.add(
        Holding(
            user_id=user.id,
            name="Cash",
            pricing_mode="manual",
            currency="USD",
            asset_class="CASH_EQUIV",
            current_value=Decimal("100"),
        )
    )
    session.commit()
    return user


def weekday_kwargs() -> dict[str, Any]:
    # Exercise the real configured dispatch, including its cadence arguments.
    return next(
        entry["kwargs"]
        for name, entry in celery_app.conf.beat_schedule.items()
        if name.startswith("report-incremental-") and entry["kwargs"]["trigger_hour"] == 17
    )


@pytest.mark.parametrize(
    "day,nodes",
    [(7, ["daily_close", "after_close"]), (6, ["daily_close"])],
    ids=["wednesday", "tuesday"],
)
def test_daily_acceptance_5_merged_batch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, day: int, nodes: list[str]
) -> None:
    daily = subscriber(db_session, "daily")
    mwf = subscriber(db_session, "mwf")
    ids = [daily.id, mwf.id]
    now = datetime(2026, 10, day, 17, tzinfo=ET)
    freeze(monkeypatch, now)

    def generate(session: Session, **kwargs: Any) -> Report:
        report = Report(
            user_id=kwargs["user_id"],
            report_date=now.date(),
            report_type=kwargs["report_type"],
            session_node=kwargs["session_node"],
            status="success",
            period_end=kwargs["now"],
        )
        session.add(report)
        session.commit()
        return report

    with (
        patch("app.services.report_generator.generate_report", side_effect=generate) as gen,
        patch.object(s, "run_cadence_checks", wraps=s.run_cadence_checks) as checks,
    ):
        result = task.generate_incremental_report.run(**weekday_kwargs())
    assert result["status"] == "completed"
    assert [call.kwargs["session_node"] for call in gen.call_args_list] == nodes
    assert [call.kwargs["user_id"] for call in gen.call_args_list] == ids[: len(nodes)]
    assert all(call.kwargs["now"] == now.astimezone(UTC) for call in gen.call_args_list)
    assert all(
        call.kwargs["now"] is gen.call_args_list[0].kwargs["now"] for call in gen.call_args_list
    )
    assert all(
        call.kwargs["moves_cache"] is gen.call_args_list[0].kwargs["moves_cache"]
        for call in gen.call_args_list
    )
    reports = list(
        db_session.scalars(
            select(Report).where(Report.user_id.in_(ids)).order_by(Report.session_node)
        )
    )
    assert sorted(r.session_node for r in reports) == sorted(nodes)
    due = ["daily", "mwf"] if day == 7 else ["daily"]
    assert [(call.args[1], call.kwargs["statuses"]) for call in checks.call_args_list] == [
        (c, statuses) for statuses in [("expired",), ("active", "expired")] for c in due
    ]


def test_daily_acceptance_6_expired_recovery_same_batch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = subscriber(db_session, "daily", "expired")
    uid = user.id
    now = datetime(2026, 10, 6, 17, tzinfo=ET)
    freeze(monkeypatch, now)

    def generate(session: Session, **kwargs: Any) -> Report:
        row = session.get(User, uid)
        assert row is not None and row.subscription_status == "active"
        assert row.subscription_expires_on == date(2026, 11, 6)
        assert row.credit_gift_balance == Decimal("2.51")
        assert kwargs["user_id"] == uid and kwargs["session_node"] == "daily_close"
        return Report(id=uid, status="success")

    with (
        patch("app.services.report_generator.generate_report", side_effect=generate) as gen,
        patch.object(s, "run_cadence_checks", wraps=s.run_cadence_checks) as checks,
    ):
        result = task.generate_incremental_report.run(**weekday_kwargs())
    assert result["status"] == "completed"
    gen.assert_called_once()
    assert [(call.args[1], call.kwargs["statuses"]) for call in checks.call_args_list] == [
        ("daily", ("expired",)),
        ("daily", ("active", "expired")),
    ]
    assert subscription_rows(db_session, user) == [
        ("gift", Decimal("-2.49"), "subscription", f"subscription:{uid}:2026-10-06:daily")
    ]


def test_daily_acceptance_7_stale_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze(monkeypatch, datetime(2026, 10, 7, 17, 31, tzinfo=ET))
    with (
        patch("app.services.report_generator.generate_report") as gen,
        patch.object(s, "run_cadence_checks") as checks,
    ):
        assert task.generate_incremental_report.run(**weekday_kwargs()) == {
            "status": "skipped_stale_trigger"
        }
    gen.assert_not_called()
    checks.assert_not_called()


@pytest.mark.parametrize(
    "month,friday,tuesday,monday,wednesday",
    [(10, 9, 6, 12, 7), (11, 6, 3, 9, 4)],
    ids=["dst", "standard-time"],
)
def test_daily_acceptance_8_next_occurrence(
    month: int, friday: int, tuesday: int, monday: int, wednesday: int
) -> None:
    result = next_occurrence_for_cadence("daily", datetime(2026, month, friday, 18, tzinfo=ET))
    assert (result.date(), result.hour, result.minute, result.tzinfo) == (
        date(2026, month, monday),
        17,
        0,
        ET,
    )
    result = next_occurrence_for_cadence("mwf", datetime(2026, month, tuesday, 18, tzinfo=ET))
    assert (result.date(), result.hour, result.minute, result.tzinfo) == (
        date(2026, month, wednesday),
        17,
        0,
        ET,
    )


def test_daily_acceptance_9_beat_entry_names() -> None:
    entries = {
        name for name in celery_app.conf.beat_schedule if name.startswith("report-incremental-")
    }
    assert entries == {"report-incremental-weekday", "report-incremental-weekly"}


@pytest.mark.parametrize("day", [10, 11])
def test_daily_weekend_has_no_due_cadence(monkeypatch: pytest.MonkeyPatch, day: int) -> None:
    freeze(monkeypatch, datetime(2026, 10, day, 17, tzinfo=ET))
    with (
        patch("app.services.report_generator.generate_report") as gen,
        patch.object(s, "run_cadence_checks") as checks,
    ):
        assert task.generate_incremental_report.run(**weekday_kwargs()) == {
            "status": "no_due_cadence",
            "results": [],
        }
    gen.assert_not_called()
    checks.assert_not_called()


def test_daily_holiday_quote_first_report(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = funded_user(db_session)
    now = datetime(2026, 10, 10, 12, tzinfo=ET)
    with patch.object(s, "datetime", wraps=datetime) as clock:
        clock.now.return_value = now
        quote = s.quote(db_session, user.id, now.date(), "daily")
    assert quote.first_report_at == datetime(2026, 10, 12, 17, tzinfo=ET)
    assert quote.fee == "2.49"


def test_daily_weekday_batch_persists_cadences(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    subscriber(db_session, "daily")
    subscriber(db_session, "mwf")
    freeze(monkeypatch, datetime(2026, 10, 7, 17, tzinfo=ET))
    task_id = str(uuid4())
    task.generate_incremental_report.push_request(id=task_id, retries=0)
    try:
        with patch(
            "app.services.report_generator.generate_report",
            return_value=Report(id=uuid4(), status="success"),
        ) as generate:
            result = task.generate_incremental_report.run(**weekday_kwargs())
        assert result["status"] == "completed"
        assert [call.kwargs["session_node"] for call in generate.call_args_list] == [
            "daily_close",
            "after_close",
        ]
    finally:
        task.generate_incremental_report.pop_request()
    event = db_session.scalars(
        select(OperationalEvent).where(
            OperationalEvent.task_id == task_id,
            OperationalEvent.operation == "report.batch",
            OperationalEvent.event_kind == "start",
        )
    ).one()
    assert event.attributes["cadences"] == ["daily", "mwf"]
