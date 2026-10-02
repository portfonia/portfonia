from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.intel import IntelSlotRun
from app.models.paid_intel import IntelArticle, IntelArticleLink, PaidApiUsage
from app.services.instrument_news_sources import CollectedItem
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_leads import select_leads, url_key
from app.services.intel_records import build_article_record, sweep_intel
from app.services.intel_selection import WorkUnit
from app.services.paid_search import ParallelClient, TavilyClient
from app.services.paid_usage import PaidUsage
from app.tests.test_intel_deepen_rules import NOW


def slot(session: Session) -> IntelSlotRun:
    row = IntelSlotRun(
        slot="post_close", run_date=NOW.date(), started_at=NOW, status="running", details={}
    )
    session.add(row)
    session.flush()
    return row


def test_06_lead_order_and_domains(db_session: Session) -> None:
    unit = WorkUnit("mover", "AAA", window_start=NOW.date() - timedelta(days=2))
    items = [
        CollectedItem("AAA agreement", NOW - timedelta(minutes=i), f"https://{host}/{i}")
        for i, host in enumerate(
            ["reuters.com", "reuters.com", "cnbc.com", "ft.com", "finance.yahoo.com"]
        )
    ]
    leads = select_leads(
        db_session, unit, items, ["AAA"], load_intel_deepen_config(), NOW, NOW - timedelta(hours=24)
    )
    assert [x.url for x in leads] == [items[0].url, items[2].url]


def test_07_finnhub_headers_only(db_session: Session) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(302, headers={"Location": "https://example.com/a"})

    with patch(
        "app.services.intel_leads.httpx.Client",
        return_value=httpx.Client(transport=httpx.MockTransport(handler)),
    ):
        leads = select_leads(
            db_session,
            WorkUnit("mover", "AAA"),
            [
                CollectedItem(
                    "AAA agreement", NOW, "https://finnhub.io/a", url_kind="finnhub_redirect"
                )
            ],
            ["AAA"],
            load_intel_deepen_config(),
            NOW,
            NOW - timedelta(hours=24),
        )
    assert calls == ["GET"] and leads[0].url == "https://example.com/a"


def test_08_accepted_seven_day_skip(db_session: Session) -> None:
    run = slot(db_session)
    item = CollectedItem("AAA agreement", NOW, "https://example.com/a")
    row = IntelArticle(
        slot_run_id=run.id,
        provider="tavily",
        url_key=url_key(item.url),
        status="accepted",
        record=build_article_record(item.title, item.published_at, NOW, "x" * 400),
        fetched_at=NOW - timedelta(days=3),
    )
    db_session.add(row)
    db_session.flush()
    args = (
        db_session,
        WorkUnit("mover", "AAA"),
        [item],
        ["AAA"],
        load_intel_deepen_config(),
        NOW,
        NOW - timedelta(hours=24),
    )
    assert not select_leads(*args)
    row.fetched_at = NOW - timedelta(days=8)
    db_session.flush()
    assert select_leads(*args)


def test_10_record_and_rejected_storage(db_session: Session) -> None:
    run = slot(db_session)
    record = build_article_record("AAA agreement", NOW, NOW, "x" * 400)
    assert set(record) == {"v", "kind", "title", "published_at", "fetched_at", "body"}
    db_session.add(
        IntelArticle(
            slot_run_id=run.id,
            provider="tavily",
            url_key="key",
            status="rejected",
            reject_reason="too_short",
            record=None,
            fetched_at=NOW,
        )
    )
    db_session.flush()
    assert db_session.scalars(select(IntelArticle)).one().record is None


def test_11_budget_truncation_and_month_limit(db_session: Session) -> None:
    run = slot(db_session)
    settings = get_settings().model_copy(update={"TAVILY_RUN_CREDIT_CAP": 3})
    with patch("app.services.paid_usage.send_ops_alert", return_value=True) as alert:
        usage = PaidUsage(db_session, run, settings, False, NOW)
        reservation = usage.reserve("tavily", Decimal(1))
        assert reservation is not None
        usage.complete(reservation, "search", 1, Decimal(".008"), 200)
        assert usage.extract_size("tavily", 12) == 10
        db_session.add(
            PaidApiUsage(
                provider="tavily",
                operation="extract",
                units=998,
                cost_usd=Decimal("7.984"),
                http_status=200,
                created_at=NOW,
            )
        )
        db_session.flush()
        usage = PaidUsage(db_session, run, settings, False, NOW)
        assert usage.extract_size("tavily", 10) == 5
        reservation = usage.reserve("tavily", Decimal(1))
        assert reservation
        usage.complete(reservation, "extract", 1, Decimal(".008"), 200)
        assert usage.reserve("tavily", Decimal(1)) is None
        assert usage.reserve("tavily", Decimal(1)) is None
        assert (
            sum("limit" in c.kwargs.get("idempotency_key", "") for c in alert.call_args_list) == 1
        )


def test_13_monthly_warning_dedup(db_session: Session) -> None:
    run = slot(db_session)
    settings = get_settings().model_copy(update={"TAVILY_MONTHLY_CREDIT_LIMIT": 5})
    with patch("app.services.paid_usage.send_ops_alert", return_value=True) as alert:
        usage = PaidUsage(db_session, run, settings, False, NOW)
        for _ in range(5):
            r = usage.reserve("tavily", Decimal(1))
            assert r
            usage.complete(r, "search", 1, Decimal(".008"), 200)
        assert sum(c.kwargs["severity"] == "WARNING" for c in alert.call_args_list) == 1
        next_month = NOW.replace(month=11)
        next_run = IntelSlotRun(
            slot="post_close",
            run_date=next_month.date(),
            started_at=next_month,
            status="running",
            details={},
        )
        db_session.add(next_run)
        db_session.flush()
        usage = PaidUsage(db_session, next_run, settings, False, next_month)
        for _ in range(4):
            r = usage.reserve("tavily", Decimal(1))
            assert r
            usage.complete(r, "search", 1, Decimal(".008"), 200)
        assert sum(c.kwargs["severity"] == "WARNING" for c in alert.call_args_list) == 2


def test_15_paid_response_units_and_ledger(db_session: Session) -> None:
    run = slot(db_session)
    usage = PaidUsage(db_session, run, get_settings(), False, NOW)
    with patch(
        "app.services.paid_search.post",
        return_value=httpx.Response(200, json={"results": []}),
    ):
        p = ParallelClient(get_settings(), 3).extract(
            ["https://example.com/" + str(i) for i in range(4)], "AAA"
        )
        t = TavilyClient(get_settings(), 3).extract(
            ["https://example.com/" + str(i) for i in range(7)], "AAA"
        )
    for provider, result in [("parallel", p), ("tavily", t)]:
        r = usage.reserve(provider, result.budget_amount(provider))
        assert r
        usage.complete(r, "extract", result.units, result.cost_usd, result.http_status)
    rows = {r.provider: r for r in db_session.scalars(select(PaidApiUsage))}
    assert rows["parallel"].units == 4 and rows["parallel"].cost_usd == Decimal(".004")
    assert rows["tavily"].units == 2 and rows["tavily"].cost_usd == Decimal(".016")


def test_paid_vendor_body_mapping() -> None:
    with patch(
        "app.services.paid_search.post",
        return_value=httpx.Response(
            200,
            json={"results": [{"url": "https://example.com/a", "raw_content": "Agreement " * 50}]},
        ),
    ):
        result = TavilyClient(get_settings(), 3).extract(["https://example.com/a"], "AAA agreement")
    assert result.bodies["https://example.com/a"] == "Agreement " * 50


def test_22_concurrent_reservations(db_session: Session) -> None:
    usage = PaidUsage(
        db_session,
        slot(db_session),
        get_settings().model_copy(update={"TAVILY_RUN_CREDIT_CAP": 2}),
        False,
        NOW,
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        reservations = list(pool.map(lambda _: usage.reserve("tavily", Decimal(1)), range(4)))
    assert sum(r is not None for r in reservations) == 2
    assert usage.skipped["tavily"] == 2


def test_24_article_retention_and_links(db_session: Session) -> None:
    run = slot(db_session)
    for days in [31, 29]:
        row = IntelArticle(
            slot_run_id=run.id,
            provider="tavily",
            url_key=str(days),
            status="rejected",
            record=None,
            fetched_at=NOW - timedelta(days=days),
        )
        db_session.add(row)
        db_session.flush()
        db_session.add(IntelArticleLink(article_id=row.id, identifier="AAA", role="mover"))
    db_session.flush()
    assert sweep_intel(db_session, NOW)["intel_articles_deleted"] == 1
    assert len(db_session.scalars(select(IntelArticleLink)).all()) == 1


def test_11_full_month_alerts_before_assignment(db_session: Session) -> None:
    db_session.add(
        PaidApiUsage(
            provider="tavily",
            operation="search",
            units=1000,
            cost_usd=8,
            http_status=200,
            created_at=NOW,
        )
    )
    db_session.flush()
    with patch("app.services.paid_usage.send_ops_alert", return_value=True) as alert:
        usage = PaidUsage(db_session, slot(db_session), get_settings(), False, NOW)
        assert "tavily" not in usage.available()
        assert "tavily" not in usage.available()
        assert sum("limit" in c.kwargs["idempotency_key"] for c in alert.call_args_list) == 1


def test_22_concurrent_paid_calls(db_session: Session) -> None:
    usage = PaidUsage(
        db_session,
        slot(db_session),
        get_settings().model_copy(update={"TAVILY_RUN_CREDIT_CAP": 2}),
        False,
        NOW,
    )
    with patch(
        "app.services.paid_search.post", return_value=httpx.Response(200, json={"results": []})
    ) as post:

        def call(_: int) -> bool:
            reservation = usage.reserve("tavily", Decimal(1))
            if reservation is None:
                return False
            TavilyClient(get_settings(), 3).search("AAA news", NOW.date(), NOW.date())
            return True

        with ThreadPoolExecutor(max_workers=4) as pool:
            sent = list(pool.map(call, range(4)))
    assert post.call_count == sum(sent) == 2
    assert usage.skipped["tavily"] == 2


def test_tavily_reported_credits_use_estimate_floor() -> None:
    for reported, expected in [(0, 1), (1, 1), (3, 3)]:
        with patch(
            "app.services.paid_search.post",
            return_value=httpx.Response(200, json={"usage": {"credits": reported}, "results": []}),
        ):
            result = TavilyClient(get_settings(), 3).extract(
                [f"https://example.com/{i}" for i in range(4)], "AAA"
            )
        assert result.units == expected
        assert result.cost_usd == Decimal(expected) * Decimal(".008")


def test_complete_settles_passed_charge(db_session: Session) -> None:
    usage = PaidUsage(db_session, slot(db_session), get_settings(), False, NOW)
    for provider, estimate, units in [("tavily", Decimal(1), 0), ("parallel", Decimal(".004"), 0)]:
        reservation = usage.reserve(provider, estimate)
        assert reservation
        usage.complete(reservation, "extract", units, Decimal(0), 500)
        row = db_session.scalars(
            select(PaidApiUsage).where(PaidApiUsage.provider == provider)
        ).one()
        assert row.units == 0
        assert row.cost_usd == 0
        assert usage.run_used[provider] == usage.month_used[provider] == 0
        assert usage.reserved[provider] == 0


def test_tavily_later_credits_are_not_added_to_prior_estimates(db_session: Session) -> None:
    usage = PaidUsage(db_session, slot(db_session), get_settings(), False, NOW)
    with patch(
        "app.services.paid_search.post",
        side_effect=[
            httpx.Response(200, json={"usage": {"credits": n}, "results": []}) for n in [0, 1]
        ],
    ):
        for _ in range(2):
            reservation = usage.reserve("tavily", Decimal(1))
            assert reservation
            result = TavilyClient(get_settings(), 3).extract(["https://example.com/a"] * 4, "AAA")
            usage.complete(reservation, "extract", result.units, result.cost_usd, 200)
    assert usage.run_used["tavily"] == 2
    assert [r.units for r in db_session.scalars(select(PaidApiUsage))] == [1, 1]
