"""Ops-only aggregation acceptance checks (#886)."""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.tests.conftest import seed_user
from app.tests.test_admin_router import _headers
from app.tests.test_host_metrics_886 import sample_values


def test_16_costs_et_groups_totals_and_auth(app_client: TestClient, db_session: Session) -> None:
    from app.models.paid_intel import PaidApiUsage
    from app.models.usage import ExternalCallDaily, LlmUsage

    at = datetime(2026, 11, 10, 23, 59, tzinfo=ET)
    uid = uuid.uuid4()
    seed_user(db_session, uid)
    for user, purpose, cost in [
        (uid, "report_pass2", Decimal("0.1")),
        (uid, "report_pass2", None),
        (None, "holdings_parse", Decimal("0.2")),
        (None, "intel_classifier", Decimal("0.3")),
        (None, "intel_classifier", None),
    ]:
        db_session.add(
            LlmUsage(
                user_id=user,
                occurred_at=at,
                purpose=purpose,
                trigger_source="scheduled",
                model="m",
                prompt_tokens=10,
                completion_tokens=2,
                cost_usd=cost,
            )
        )
    db_session.add(
        LlmUsage(
            occurred_at=at + timedelta(minutes=2),
            purpose="translation",
            trigger_source="ops",
            model="m",
            cost_usd=1,
        )
    )
    db_session.add(ExternalCallDaily(day=at.date(), provider="resend", calls=2))
    db_session.add(
        PaidApiUsage(
            provider="tavily", operation="search", units=3, cost_usd=Decimal("0.04"), created_at=at
        )
    )
    db_session.flush()
    url = "/admin/usage/costs?start=2026-11-10&end=2026-11-12"
    assert app_client.get(url).status_code == 401
    response = app_client.get(url, headers=_headers())
    assert response.status_code == 200
    data = response.json()
    assert (data["start"], data["end"]) == ("2026-11-10", "2026-11-12")
    assert [d["date"] for d in data["days"]] == ["2026-11-10", "2026-11-11"]
    day = data["days"][0]
    by = {r["purpose"]: r for r in day["llm_by_user"]}
    assert by["report_pass2"] == dict(
        user_id=str(uid),
        purpose="report_pass2",
        trigger_source="scheduled",
        calls=2,
        prompt_tokens=20,
        completion_tokens=4,
        cost_usd=0.1,
        calls_without_cost=1,
    )
    assert by["holdings_parse"]["user_id"] is None
    assert day["llm_shared"] == [
        dict(purpose="intel_classifier", calls=2, cost_usd=0.3, calls_without_cost=1)
    ]
    assert day["paid_search"] == [dict(provider="tavily", units=3, cost_usd=0.04)]
    assert day["external_calls"] == [dict(provider="resend", calls=2, cost_usd=0.0018)]
    assert day["total_cost_usd"] == 0.6418
    assert (
        app_client.get(
            "/admin/usage/costs?start=2026-11-12&end=2026-11-10", headers=_headers()
        ).status_code
        == 422
    )


def test_17_host_sorted_ratios_and_deltas(app_client: TestClient, db_session: Session) -> None:
    from app.models.usage import HostSample

    at = datetime(2026, 11, 10, 12, tzinfo=ET)
    for offset, incoming, outgoing, oom in [(10, 1, 2, 0), (0, 10, 20, 3), (5, 15, 30, 4)]:
        db_session.add(
            HostSample(
                **sample_values(
                    sampled_at=at + timedelta(minutes=offset),
                    pswpin=incoming,
                    pswpout=outgoing,
                    oom_kill=oom,
                    swap_free_bytes=750,
                )
            )
        )
    db_session.flush()
    response = app_client.get(
        "/admin/usage/host?start=2026-11-10&end=2026-11-10", headers=_headers()
    )
    assert response.status_code == 200
    rows = response.json()["samples"]
    assert len(rows) == 3 and [r["pswpin"] for r in rows] == [10, 15, 1]
    assert (
        rows[0]["mem_available_ratio"] == 0.8
        and rows[0]["swap_used_bytes"] == 250
        and rows[0]["swap_used_ratio"] == 0.25
        and rows[0]["disk_used_pct"] == 20
    )
    assert [(r["pswpin_delta"], r["pswpout_delta"], r["oom_kill_delta"]) for r in rows] == [
        (None, None, None),
        (5, 10, 1),
        (None, None, None),
    ]
    assert "id" in rows[0] and rows[0]["redis_used_memory_bytes"] == 200
    assert (
        app_client.get(
            "/admin/usage/host?start=2026-11-12&end=2026-11-10", headers=_headers()
        ).status_code
        == 422
    )
