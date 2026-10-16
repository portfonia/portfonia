"""Read-only report and intelligence pull contracts (#652)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from typing import Self, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
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
    jade(db_session)
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
                "headlines": [],
                "articles": [
                    {
                        "title": "Macro article",
                        "published_at": None,
                        "body": "Body of Macro article",
                    }
                ],
            },
            {"theme": "inflation", "headlines": [], "articles": []},
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
        assert row["item_count"] == (1 if endpoint == REPORTS else 2)
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


# #781 keeps one focused test for each contract acceptance item.
def jade(session: Session) -> None:
    user = session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_type = "jade"
    session.commit()


def freeze_intel(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    from app.services import agent_pull

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(moment.timestamp(), tz)

    monkeypatch.setattr(agent_pull, "datetime", Clock)
    monkeypatch.setattr("app.core.timezones.today_et", lambda: moment.date())


def test_781_acceptance_01_advanced_headlines(
    app_client: TestClient, db_session: Session, token: dict[str, object]
) -> None:
    seed_intel(db_session)
    report(db_session, themes=["us_rates"])
    db_session.commit()
    body = app_client.get(INTEL, params={"date": DAY.isoformat()}, headers=bearer(token)).json()
    assert body["holdings"][0]["headlines"][1]["summary"] is None
    assert body["detail"] == "headlines"
    assert body["holdings"][0]["articles"] == []
    assert body["macro"]["themes"][0]["articles"] == []


def test_781_acceptance_02_jade_preserves_material(
    app_client: TestClient, db_session: Session, token: dict[str, object]
) -> None:
    seed_intel(db_session)
    report(db_session, themes=["us_rates"])
    jade(db_session)
    body = app_client.get(INTEL, params={"date": DAY.isoformat()}, headers=bearer(token)).json()
    assert body["detail"] == "full"
    assert body["holdings"][0]["headlines"] == [
        {
            "title": "Latest headline",
            "published_at": db_session.query(News)
            .filter_by(url_hash="headline-1")
            .one()
            .published_at.isoformat(),
            "summary": None,
        },
        {
            "title": "NVDA headline",
            "published_at": db_session.query(News)
            .filter_by(url_hash="headline-0")
            .one()
            .published_at.isoformat(),
            "summary": "Details",
        },
    ]
    assert body["holdings"][0]["articles"] == [
        {"title": "Shared article", "published_at": None, "body": "Body of Shared article"}
    ]
    assert body["macro"]["themes"][0]["articles"] == [
        {"title": "Macro article", "published_at": None, "body": "Body of Macro article"}
    ]


def test_781_acceptance_03_macro_filter_and_pool_promotion(
    app_client: TestClient,
    db_session: Session,
    token: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.intel_records import headline_from_row, store_headline

    # Use the real transaction clock for the server-side promotion timestamp.
    stamp = db_session.scalar(select(func.now()))
    assert isinstance(stamp, datetime)
    stamp = stamp.astimezone(ET)
    freeze_intel(monkeypatch, stamp + timedelta(minutes=10))
    report(db_session, stamp.date(), themes=["Monetary"])
    monkeypatch.setattr(
        "app.services.macro_detector._load_keywords",
        lambda: {"Monetary": ["Fed"], "Other": ["tariff"]},
    )
    for i, label in enumerate(["development", None, "commentary", "off_topic", None, None]):
        record: dict[str, object] = {"title": "tariff" if i == 4 else f"Fed update {i}"}
        if label:
            record.update({"type": label, "event": "Fed", "importance": 3})
        row = News(
            url_hash=f"pool-{i}",
            origin="pool",
            published_at=stamp - timedelta(days=1) if i == 5 else stamp - timedelta(seconds=i),
            created_at=stamp,
            record=record,
        )
        db_session.add(row)
    db_session.commit()
    body = app_client.get(
        INTEL, params={"date": stamp.date().isoformat()}, headers=bearer(token)
    ).json()
    assert [h["title"] for h in body["macro"]["themes"][0]["headlines"]] == [
        "Fed update 0",
        "Fed update 1",
    ]
    assert all(h["summary"] is None for h in body["macro"]["themes"][0]["headlines"])
    promoted = News(
        url_hash="promoted",
        origin="instrument",
        published_at=stamp,
        created_at=stamp - timedelta(hours=2),
        record={"title": "Fed promotion"},
    )
    db_session.add(promoted)
    db_session.flush()
    store_headline(db_session, headline_from_row(promoted), "pool", "article", None)
    db_session.commit()
    body = app_client.get(
        INTEL, params={"since": (stamp - timedelta(hours=1)).isoformat()}, headers=bearer(token)
    ).json()
    assert "Fed promotion" in [h["title"] for h in body["macro"]["themes"][0]["headlines"]]


def test_781_acceptance_04_since_window_and_elapsed_dst(
    app_client: TestClient,
    db_session: Session,
    token: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_intel(monkeypatch, NOW)
    seed_intel(db_session)
    links = db_session.scalars(select(NewsInstrument).order_by(NewsInstrument.id)).all()
    for link in links:
        link.created_at = NOW - timedelta(days=2)
    links[0].created_at = NOW - timedelta(hours=25)
    selected = db_session.get(News, links[0].news_id)
    assert selected is not None
    expected = str(selected.record["title"]).split(" https://")[0]
    db_session.commit()
    since = NOW - timedelta(hours=26)
    response = app_client.get(INTEL, params={"since": since.isoformat()}, headers=bearer(token))
    assert response.status_code == 200
    body = response.json()
    assert [h["title"] for h in body["holdings"][0]["headlines"]] == [expected]
    assert body["next_since"] == (since + timedelta(hours=24)).isoformat()
    assert body["has_more"] is True
    body = app_client.get(
        INTEL, params={"since": (NOW - timedelta(hours=1)).isoformat()}, headers=bearer(token)
    ).json()
    assert body["next_since"] == (NOW - timedelta(minutes=5)).isoformat()
    assert body["has_more"] is False
    freeze_intel(monkeypatch, datetime(2026, 11, 2, 12, tzinfo=ET))
    body = app_client.get(
        INTEL, params={"since": "2026-10-31T12:00:00-04:00"}, headers=bearer(token)
    ).json()
    assert body["next_since"] == "2026-11-01T11:00:00-05:00"


def test_781_acceptance_05_missed_completed_batch(
    app_client: TestClient,
    db_session: Session,
    token: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_intel(monkeypatch, NOW)
    seed_intel(db_session)
    runs = db_session.scalars(select(IntelSlotRun).order_by(IntelSlotRun.run_date)).all()
    runs[0].finished_at = NOW - timedelta(hours=1)
    runs[1].status = "running"
    runs[1].finished_at = None
    jade(db_session)
    response = app_client.get(
        INTEL, params={"since": (NOW - timedelta(hours=2)).isoformat()}, headers=bearer(token)
    )
    assert response.status_code == 200
    assert response.json()["holdings"][0]["articles"] == [
        {"title": "Previous day", "published_at": None, "body": "Body of Previous day"}
    ]


def test_781_acceptance_06_mode_validation(
    app_client: TestClient, token: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_intel(monkeypatch, NOW)
    for params in [
        {"date": DAY.isoformat(), "since": NOW.isoformat()},
        {},
        {"since": NOW.replace(tzinfo=None).isoformat()},
        {"since": (NOW - timedelta(days=7)).isoformat()},
        {"since": (NOW + timedelta(seconds=1)).isoformat()},
    ]:
        assert app_client.get(INTEL, params=params, headers=bearer(token)).status_code == 422


def test_781_acceptance_07_audit_since(
    app_client: TestClient,
    db_session: Session,
    token: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_intel(monkeypatch, NOW)
    params = {"since": (NOW - timedelta(hours=1)).isoformat()}
    app_client.get(INTEL, params=params, headers=bearer(token))
    assert audit_rows(db_session)[-1]["params"] == params
