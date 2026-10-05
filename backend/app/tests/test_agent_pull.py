"""Read-only report and intelligence pull contracts (#652)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.rate_limit import InMemoryBackend, get_backend
from app.core.timezones import ET
from app.models.holding import Holding
from app.models.intel import IntelSlotRun, NewsInstrument
from app.models.news import News
from app.models.paid_intel import IntelArticle, IntelArticleLink
from app.models.report import Report
from app.models.user import User
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_agent_api import audit_rows, bearer, create

REPORTS = "/agent/v1/reports"
INTEL = "/agent/v1/intel"
NOW = datetime(2026, 10, 5, 18, tzinfo=ET)
DAY = NOW.date()


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.api_tokens.now_et", lambda: NOW)
    monkeypatch.setattr("app.core.timezones.today_et", lambda: DAY)


@pytest.fixture
def token(app_client: TestClient, db_session: Session) -> dict[str, object]:
    user = seed_user(db_session, TEST_USER_ID)
    user.subscription_status = "active"
    user.subscription_type = "daily"
    db_session.flush()
    return create(app_client)


def report(
    session: Session,
    day: date = DAY,
    *,
    user_id: UUID = TEST_USER_ID,
    node: str = "daily_close",
    status: str = "success",
    created_at: datetime = NOW,
    themes: list[str] | None = None,
) -> Report:
    row = Report(
        user_id=user_id,
        report_date=day,
        report_type="incremental",
        session_node=node,
        status=status,
        report_md=f"## Briefing {node}",
        period_start=NOW - timedelta(days=1),
        period_end=NOW,
        generated_at=NOW,
        created_at=created_at,
        report_inputs={"macro_signals": {"hits": [{"theme": t} for t in themes or []]}},
    )
    session.add(row)
    session.flush()
    return row


def range_params() -> dict[str, str]:
    return {"start": (DAY - timedelta(days=20)).isoformat(), "end": DAY.isoformat()}


def test_652_acceptance_01_newest_five_and_owner_isolation(
    app_client: TestClient,
    db_session: Session,
    token: dict[str, object],
) -> None:
    nodes = ["daily_close", "after_close", "weekend_snapshot", "manual", "legacy"]
    rows = [
        report(
            db_session,
            node=node,
            created_at=NOW - timedelta(minutes=i),
            status="skipped" if node == "manual" else "success",
        )
        for i, node in enumerate(nodes)
    ]
    rows += [report(db_session, DAY - timedelta(days=i)) for i in range(1, 8)]
    hidden = [
        report(
            db_session, node=f"hidden-{state}", status=state, created_at=NOW + timedelta(minutes=1)
        )
        for state in ("needs_review", "in_progress", "failed")
    ]
    other = uuid4()
    seed_user(db_session, other)
    foreign = report(db_session, user_id=other)
    db_session.commit()
    response = app_client.get(REPORTS, params=range_params(), headers=bearer(token))
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 5
    assert body["total_in_range"] == 12
    assert body["truncated"] is True
    assert [r["id"] for r in body["items"]] == [str(r.id) for r in rows[:5]]
    assert [r["type"] for r in body["items"]] == ["daily", "mwf", "weekly", "manual", "other"]
    assert {r["id"] for r in body["items"]}.isdisjoint(str(r.id) for r in [*hidden, foreign])
    assert set(body["items"][0]) == {
        "id",
        "report_date",
        "type",
        "status",
        "period_start",
        "period_end",
        "generated_at",
        "report_md",
    }
    assert body["items"][0]["report_md"] == rows[0].report_md
    assert body["start"] == range_params()["start"] and body["end"] == DAY.isoformat()


@pytest.mark.parametrize(
    "params",
    [
        {"start": DAY.isoformat(), "end": (DAY - timedelta(days=1)).isoformat()},
        {"start": DAY.isoformat(), "end": (DAY + timedelta(days=1)).isoformat()},
        {"end": DAY.isoformat()},
        {"start": DAY.isoformat()},
        {"start": "bad", "end": DAY.isoformat()},
    ],
)
def test_652_acceptance_02_report_validation(
    app_client: TestClient,
    token: dict[str, object],
    params: dict[str, str],
) -> None:
    assert app_client.get(REPORTS, params=params, headers=bearer(token)).status_code == 422


@pytest.mark.parametrize(
    "plan,status",
    [("weekly", "active"), ("mwf", "active"), (None, "inactive"), ("daily", "inactive")],
)
def test_652_acceptance_03_reports_all_plans_intel_advanced(
    app_client: TestClient,
    db_session: Session,
    token: dict[str, object],
    plan: str | None,
    status: str,
) -> None:
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_type = plan
    user.subscription_status = status
    db_session.commit()
    response = app_client.get(REPORTS, params=range_params(), headers=bearer(token))
    assert response.status_code == 200
    assert response.json()["items"] == [] and response.json()["total_in_range"] == 0
    assert response.json()["truncated"] is False
    denied = app_client.get(INTEL, params={"date": DAY.isoformat()}, headers=bearer(token))
    assert denied.status_code == 403
    assert denied.json() == {"detail": "subscription_required"}


@pytest.mark.parametrize("offset,status", [(-7, 422), (-6, 200), (0, 200), (1, 422)])
def test_652_acceptance_04_et_seven_day_boundary(
    app_client: TestClient,
    token: dict[str, object],
    offset: int,
    status: int,
) -> None:
    response = app_client.get(
        INTEL, params={"date": (DAY + timedelta(days=offset)).isoformat()}, headers=bearer(token)
    )
    assert response.status_code == status


def seed_intel(session: Session) -> None:
    # Deliberately insert the later ticker first; attribution follows position.
    session.add_all(
        [
            Holding(
                user_id=TEST_USER_ID,
                name="Apple",
                ticker="AAPL",
                position=2,
                pricing_mode="auto",
                currency="USD",
            ),
            Holding(
                user_id=TEST_USER_ID,
                name="NVIDIA",
                ticker="nvda",
                position=0,
                pricing_mode="auto",
                currency="USD",
            ),
            Holding(
                user_id=TEST_USER_ID,
                name="Fund",
                fund_code="110011",
                position=1,
                pricing_mode="auto",
                currency="CNY",
            ),
            Holding(
                user_id=TEST_USER_ID,
                name="Duplicate NVIDIA",
                ticker="NVDA",
                position=3,
                pricing_mode="auto",
                currency="USD",
            ),
            Holding(
                user_id=TEST_USER_ID,
                name="Empty",
                ticker="EMPTY",
                position=4,
                pricing_mode="auto",
                currency="USD",
            ),
        ]
    )
    for i, (stamp, title, summary) in enumerate(
        [
            (
                NOW.replace(hour=0),
                "NVDA headline https://example.com/title",
                "Details http://example.com/details",
            ),
            (NOW.replace(hour=23, minute=59), "Latest headline", None),
            (NOW.replace(hour=0) - timedelta(seconds=1), "Yesterday headline", "Old"),
            (NOW.replace(hour=0) + timedelta(days=1), "Tomorrow headline", None),
        ]
    ):
        row = News(
            url_hash=f"headline-{i}",
            origin="instrument",
            published_at=stamp,
            record={
                "title": title,
                "summary": summary,
                "url": "https://example.com/private",
                "source": "Excluded",
            },
        )
        session.add(row)
        session.flush()
        session.add(NewsInstrument(news_id=row.id, identifier="NVDA"))
    runs = []
    for days in (0, 1):
        run = IntelSlotRun(
            slot="post_close",
            run_date=DAY - timedelta(days=days),
            started_at=NOW - timedelta(days=days),
            status="ok",
        )
        session.add(run)
        session.flush()
        runs.append(run)
    for i, (title, key, provider, status, owners, run) in enumerate(
        [
            (
                "Shared article",
                "shared",
                "tavily",
                "accepted",
                ["NVDA", "AAPL", "theme:us_rates"],
                runs[0],
            ),
            ("Duplicate provider", "shared", "parallel", "accepted", ["AAPL"], runs[0]),
            ("Rejected", "rejected", "tavily", "rejected", ["NVDA"], runs[0]),
            ("Previous day", "previous", "tavily", "accepted", ["NVDA"], runs[1]),
            (
                "Macro article",
                "macro",
                "tavily",
                "accepted",
                ["theme:us_rates", "theme:inflation"],
                runs[0],
            ),
            ("Other theme", "unrelated", "tavily", "accepted", ["theme:unrelated"], runs[0]),
            ("Malformed", "malformed", "tavily", "accepted", ["NVDA"], runs[0]),
        ]
    ):
        article = IntelArticle(
            slot_run_id=run.id,
            provider=provider,
            url_key=key,
            status=status,
            fetched_at=NOW - timedelta(minutes=i),
            record=(
                {
                    "title": title,
                    "body": f"Body of {title}",
                    "published_at": None,
                    "url": "https://example.com/private",
                    "provider": "Excluded",
                }
                if status == "accepted"
                else None
            ),
        )
        if title == "Malformed":
            article.record = {"title": 7, "body": "Bad"}
        session.add(article)
        session.flush()
        for owner in owners:
            macro = owner.startswith("theme:")
            session.add(
                IntelArticleLink(
                    article_id=article.id,
                    theme=owner[6:] if macro else None,
                    identifier=None if macro else owner,
                    role="macro" if macro else "mover",
                )
            )
    session.flush()


def assert_no_sources(value: object) -> None:
    if isinstance(value, dict):
        assert not {"url", "url_key", "provider", "source", "publisher"}.intersection(value)
        for child in value.values():
            assert_no_sources(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_sources(child)
    elif isinstance(value, str):
        assert "http://" not in value and "https://" not in value


def test_652_acceptance_05_intel_material_global_dedupe_and_url_projection(
    app_client: TestClient,
    db_session: Session,
    token: dict[str, object],
) -> None:
    seed_intel(db_session)
    chosen = report(
        db_session, DAY - timedelta(days=1), themes=["us_rates", "inflation", "us_rates"]
    )
    report(db_session, DAY, status="needs_review", themes=["unrelated"])
    other = uuid4()
    seed_user(db_session, other)
    report(db_session, user_id=other, themes=["unrelated"])
    db_session.commit()
    response = app_client.get(INTEL, params={"date": DAY.isoformat()}, headers=bearer(token))
    assert response.status_code == 200
    body = response.json()
    assert body["date"] == DAY.isoformat()
    assert [h["identifier"] for h in body["holdings"]] == ["NVDA"]
    headlines = body["holdings"][0]["headlines"]
    assert [h["title"] for h in headlines] == ["Latest headline", "NVDA headline"]
    assert headlines[0]["summary"] is None and headlines[1]["summary"] == "Details"
    assert body["holdings"][0]["articles"] == [
        {"title": "Shared article", "published_at": None, "body": "Body of Shared article"}
    ]
    assert body["macro"] == {
        "source_report_id": str(chosen.id),
        "source_report_date": chosen.report_date.isoformat(),
        "themes": [
            {
                "theme": "us_rates",
                "articles": [
                    {
                        "title": "Macro article",
                        "published_at": None,
                        "body": "Body of Macro article",
                    }
                ],
            },
            {"theme": "inflation", "articles": []},
        ],
    }
    assert_no_sources(body)
    assert audit_rows(db_session)[-1]["item_count"] == 4
    stored = db_session.query(News).filter_by(url_hash="headline-0").one()
    assert "https://" in str(stored.record["title"]) and "http://" in str(stored.record["summary"])


@pytest.mark.parametrize("skipped", [False, True])
def test_652_acceptance_06_macro_missing_or_latest_empty_skipped(
    app_client: TestClient,
    db_session: Session,
    token: dict[str, object],
    skipped: bool,
) -> None:
    if skipped:
        report(db_session, DAY - timedelta(days=2), themes=["us_rates"])
        latest = report(db_session, DAY - timedelta(days=1), status="skipped")
    else:
        other = uuid4()
        seed_user(db_session, other)
        report(db_session, user_id=other, themes=["us_rates"])
        report(db_session, DAY + timedelta(days=1), themes=["us_rates"])
    db_session.commit()
    response = app_client.get(INTEL, params={"date": DAY.isoformat()}, headers=bearer(token))
    assert response.status_code == 200
    assert response.json()["macro"] == (
        {
            "source_report_id": str(latest.id),
            "source_report_date": latest.report_date.isoformat(),
            "themes": [],
        }
        if skipped
        else None
    )


@pytest.mark.parametrize("endpoint", [REPORTS, INTEL])
def test_652_acceptance_07_audit_items_dates_and_hourly_limits(
    app_client: TestClient,
    db_session: Session,
    token: dict[str, object],
    endpoint: str,
) -> None:
    report(db_session)
    seed_intel(db_session)
    db_session.commit()
    params = range_params() if endpoint == REPORTS else {"date": DAY.isoformat()}
    store = cast(InMemoryBackend, get_backend())
    for _ in range(20):
        response = app_client.get(endpoint, params=params, headers=bearer(token))
        assert response.status_code == 200
        store.advance(61)
    rows = audit_rows(db_session)
    assert len(rows) == 20
    for row in rows:
        assert row["endpoint"] == endpoint
        assert row["status_code"] == 200
        assert row["item_count"] == (1 if endpoint == REPORTS else 3)
        assert row["params"] == params
    refused = app_client.get(endpoint, params=params, headers=bearer(token))
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0
    assert audit_rows(db_session)[-1]["status_code"] == 429


def test_652_intel_audit_date_nul_is_sanitized(
    app_client: TestClient,
    db_session: Session,
    token: dict[str, object],
) -> None:
    response = app_client.get(INTEL, params={"date": "\x00"}, headers=bearer(token))
    assert response.status_code == 422
    assert audit_rows(db_session)[-1]["params"] == {"date": "\ufffd"}
