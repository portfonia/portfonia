"""Independent accounting and retention acceptance checks for #886."""

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.core.timezones import ET, today_et
from app.services.report_llm import _call_llm
from app.services.usage_recording import LlmUsageContext
from app.tests.conftest import seed_user


def response(cost: bool = True) -> SimpleNamespace:
    usage: dict[str, int | float] = dict(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    if cost:
        usage["cost"] = 0.012345
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
        usage=SimpleNamespace(**usage),
        model="returned/model",
    )


def call(ctx: LlmUsageContext | None = None, cost: bool = True) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = response(cost)
    _call_llm(client, "requested/model", "system", "text", usage_ctx=ctx)
    return client


def test_01_llm_row_survives_business_rollback(db_session: Session) -> None:
    from app.services.usage_recording import LlmUsageContext

    uid = uuid.uuid4()
    with Session(get_engine()) as seed:
        seed_user(seed, uid)
        seed.commit()
    try:
        ctx = LlmUsageContext(uid, "report_pass2", "scheduled")
        call(ctx)
        db_session.rollback()
        with get_engine().connect() as conn:
            row = conn.execute(text("SELECT * FROM llm_usage WHERE user_id=:u"), {"u": uid}).one()
        assert (
            row.user_id,
            row.purpose,
            row.trigger_source,
            row.model,
            row.prompt_tokens,
            row.completion_tokens,
            row.cost_usd,
        ) == (uid, "report_pass2", "scheduled", "requested/model", 100, 20, Decimal("0.012345"))
    finally:
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM llm_usage WHERE user_id=:u"), {"u": uid})
            conn.execute(text("DELETE FROM users WHERE id=:u"), {"u": uid})


def test_02_usage_requested() -> None:
    assert call().chat.completions.create.call_args.kwargs["extra_body"]["usage"] == {
        "include": True
    }


def test_03_missing_cost_is_null(db_session: Session) -> None:
    from app.services.usage_recording import LlmUsageContext

    model = "missing-cost-" + str(uuid.uuid4())
    client = MagicMock()
    client.chat.completions.create.return_value = response(False)
    _call_llm(
        client,
        model,
        "system",
        "text",
        usage_ctx=LlmUsageContext(None, "intel_classifier", "scheduled"),
    )
    with get_engine().begin() as conn:
        row = conn.execute(
            text("DELETE FROM llm_usage WHERE model=:m RETURNING cost_usd"), {"m": model}
        ).one()
    assert row.cost_usd is None


def test_04_sink_failure_warns_without_raising(caplog: pytest.LogCaptureFixture) -> None:
    from app.services import usage_recording as ur

    logging.getLogger(ur.__name__).disabled = False
    with (
        patch.object(ur, "_get_sink_engine", side_effect=RuntimeError("database unavailable")),
        caplog.at_level(logging.WARNING),
    ):
        ur.record_llm_call(
            ur.LlmUsageContext(None, "intel_classifier", "scheduled"), "m", 1, 2, None
        )
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING


def test_10_external_upsert_same_et_day(db_session: Session) -> None:
    from app.services import usage_recording as ur

    day = today_et() - timedelta(days=42)
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM external_call_daily WHERE day=:d"), {"d": day})
    with patch.object(ur, "today_et", return_value=day):
        ur.count_external_call("resend")
        ur.count_external_call("resend")
    with get_engine().begin() as conn:
        row = conn.execute(
            text("DELETE FROM external_call_daily WHERE day=:d RETURNING provider,calls"),
            {"d": day},
        ).one()
    assert tuple(row) == ("resend", 2)


def test_11_user_deletion_keeps_usage(db_session: Session) -> None:
    from app.models.usage import LlmUsage

    uid = uuid.uuid4()
    seed_user(db_session, uid)
    row = LlmUsage(user_id=uid, purpose="holdings_parse", trigger_source="user", model="m")
    db_session.add(row)
    db_session.flush()
    db_session.execute(text("DELETE FROM users WHERE id=:u"), {"u": uid})
    db_session.expire_all()
    retained = db_session.get(LlmUsage, row.id)
    assert retained is not None and retained.user_id is None


def test_12_daily_cleanup_retains_newer_rows(db_session: Session) -> None:
    from app.models.usage import ExternalCallDaily, HostSample, LlmUsage
    from app.tasks.operational_events_tasks import cleanup_operational_events
    from app.tests.test_host_metrics_886 import sample_values

    old, new = datetime.now(ET) - timedelta(days=91), datetime.now(ET) - timedelta(days=1)
    for when in (old, new):
        db_session.add(
            LlmUsage(
                occurred_at=when,
                purpose="intel_classifier",
                trigger_source="scheduled",
                model="retention",
            )
        )
        db_session.add(HostSample(**sample_values(sampled_at=when)))
        db_session.add(ExternalCallDaily(day=when.date(), provider="fred", calls=1))
    db_session.flush()
    cleanup_operational_events.run()
    for table, column in [
        ("llm_usage", "occurred_at"),
        ("host_samples", "sampled_at"),
        ("external_call_daily", "day"),
    ]:
        assert (
            db_session.execute(
                text(f"SELECT count(*) FROM {table} WHERE {column}<:cutoff"),
                {"cutoff": datetime.now(ET) - timedelta(days=90)},
            ).scalar()
            == 0
        )
        assert db_session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() >= 1
