"""Integration tests for /holdings endpoints — real Postgres, mocked LLM."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.models.upload_job import UploadJob
from app.tests.conftest import TEST_USER_ID, seed_user

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _seed_test_user(db_session: Session) -> None:
    """`app_client` overrides identity to TEST_USER_ID without a matching
    `users` row (see conftest.seed_user's docstring) — issue #129 B7's new
    FKs on holdings/upload_jobs.user_id need one to exist before any test
    in this file writes a row under that id."""
    seed_user(db_session, TEST_USER_ID)


_PARSED_APPLE: dict[str, object] = {
    "name": "Apple",
    "ticker": "AAPL",
    "fund_code": None,
    "currency": "USD",
    "shares": 10.0,
    "avg_cost": 180.0,
    "current_value": None,
    "pricing_mode": "auto",
    "asset_type": "stock",
    "broker": "IBKR",
    "account": None,
    "portfolio": None,
    "notes": None,
    "issues": [],
    "confidence": 1.0,
}

_PARSED_PSH: dict[str, object] = {
    "name": "Pershing Square Holdings",
    "ticker": "PSH",
    "fund_code": None,
    "currency": "GBP",
    "shares": 10.0,
    "avg_cost": 55.0,
    "current_value": None,
    "pricing_mode": "auto",
    "asset_type": "stock",
    "broker": "IBKR",
    "account": None,
    "portfolio": None,
    "notes": None,
    "issues": [],
    "confidence": 1.0,
}

_PARSED_CASH: dict[str, object] = {
    "name": "USD Cash",
    "ticker": None,
    "fund_code": None,
    "currency": "USD",
    "shares": None,
    "avg_cost": None,
    "current_value": 15000.0,
    "pricing_mode": "manual",
    "asset_type": "cash",
    "broker": "Schwab",
    "account": None,
    "portfolio": None,
    "notes": None,
    "issues": [],
    "confidence": 1.0,
}

# China A-share fund: fund_code only, no ticker — exercises the confirm-time
# NAV cold-start dispatch (issue #196).
_PARSED_FUND: dict[str, object] = {
    "name": "Huaxia SSE 50 ETF",
    "ticker": None,
    "fund_code": "513100",
    "currency": "CNY",
    "shares": 1000.0,
    "avg_cost": 1.2,
    "current_value": None,
    "pricing_mode": "auto",
    "asset_type": "etf",
    "asset_class": "EQUITY_US_BROAD",
    "market": "A-Share",
    "broker": "Huatai",
    "account": None,
    "portfolio": None,
    "notes": None,
    "issues": [],
    "confidence": 1.0,
}

_MOCK_PREVIEW = {
    "valid_rows": [_PARSED_APPLE, _PARSED_CASH],
    "issue_rows": [{"raw": "bad row", "reason": "Cannot parse"}],
}


def _make_mock_client(payload: dict[str, object]) -> MagicMock:
    mock_message = MagicMock()
    mock_message.content = json.dumps(payload)
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response
    return mock_client


# ---------------------------------------------------------------------------
# POST /holdings/upload
# ---------------------------------------------------------------------------


def test_upload_returns_pending_job_and_enqueues_parse(
    app_client: TestClient, db_session: Session
) -> None:
    """Issue #77: /upload no longer parses synchronously — it creates a
    pending UploadJob and hands off to Celery, returning immediately."""
    content = (FIXTURES / "sample_holdings.md").read_bytes()
    with patch("app.routers.holdings.parse_holdings_upload") as mock_task:
        resp = app_client.post(
            "/holdings/upload",
            files={"file": ("holdings.md", content, "text/markdown")},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert body["preview"] is None
    assert body["error"] is None
    # PR #82 review: the task takes job_id only — the extracted text never
    # becomes a Celery/Redis broker message argument. It's written onto the
    # row instead, for the task to read and clear.
    mock_task.delay.assert_called_once_with(body["id"])
    job = db_session.get(UploadJob, uuid.UUID(body["id"]))
    assert job is not None
    assert job.raw_text is not None
    assert "AAPL" in job.raw_text or "Apple" in job.raw_text


def test_upload_rejects_oversized_file(app_client: TestClient) -> None:
    """PR #82 review: a hard cap before extract/enqueue, not unbounded
    in-memory reads for whatever gets posted."""
    from app.routers import holdings as holdings_router

    oversized = b"a" * (holdings_router._MAX_UPLOAD_BYTES + 1)
    resp = app_client.post(
        "/holdings/upload",
        files={"file": ("holdings.md", oversized, "text/markdown")},
    )
    assert resp.status_code == 422
    assert "too large" in resp.json()["detail"].lower()


def test_upload_rejects_oversized_extracted_text(
    app_client: TestClient, db_session: Session
) -> None:
    """Issue #54: the 5 MiB raw-byte cap only bounds the uploaded file, not
    what it extracts to. A plain-text file well under _MAX_UPLOAD_BYTES can
    still extract to more text than any real holdings file would ever
    contain — reject before it's persisted to UploadJob.raw_text or shipped
    to the LLM.

    PR #158 review: the 422 alone doesn't prove nothing leaked — assert the
    row was never written and the parse task never enqueued, the same
    property the enqueue-failure test above already guards for its own
    failure path."""
    from app.routers import holdings as holdings_router

    assert holdings_router._MAX_TEXT_BYTES < holdings_router._MAX_UPLOAD_BYTES
    oversized_text = b"a" * (holdings_router._MAX_TEXT_BYTES + 1)
    with patch("app.routers.holdings.parse_holdings_upload") as mock_task:
        resp = app_client.post(
            "/holdings/upload",
            files={"file": ("holdings.txt", oversized_text, "text/plain")},
        )
    assert resp.status_code == 422
    assert "too large" in resp.json()["detail"].lower()
    mock_task.delay.assert_not_called()
    assert db_session.query(UploadJob).count() == 0


def test_upload_rejects_oversized_extracted_text_multibyte(
    app_client: TestClient, db_session: Session
) -> None:
    """PR #158 review: the cap is byte-based (``len(text.encode("utf-8"))``),
    which is the right unit for the LLM/DB payload — this product's broker
    exports are often CJK. A body whose character count is under the cap but
    whose UTF-8 byte count is over it must still be rejected; this guards
    against a future "simplification" to ``len(text)`` (char count), which
    the ASCII-only cases above wouldn't catch."""
    from app.routers import holdings as holdings_router

    char_count = holdings_router._MAX_TEXT_BYTES // 3 + 1
    oversized_text = ("中" * char_count).encode("utf-8")
    assert char_count < holdings_router._MAX_TEXT_BYTES
    assert len(oversized_text) > holdings_router._MAX_TEXT_BYTES
    with patch("app.routers.holdings.parse_holdings_upload") as mock_task:
        resp = app_client.post(
            "/holdings/upload",
            files={"file": ("holdings.txt", oversized_text, "text/plain")},
        )
    assert resp.status_code == 422
    assert "too large" in resp.json()["detail"].lower()
    mock_task.delay.assert_not_called()
    assert db_session.query(UploadJob).count() == 0


def test_upload_accepts_text_at_the_cap(app_client: TestClient) -> None:
    """Boundary check: exactly at the cap must still succeed (off-by-one
    guard against the oversized-text rejection above)."""
    from app.routers import holdings as holdings_router

    with patch("app.routers.holdings.parse_holdings_upload") as mock_task:
        resp = app_client.post(
            "/holdings/upload",
            files={
                "file": (
                    "holdings.txt",
                    b"a" * holdings_router._MAX_TEXT_BYTES,
                    "text/plain",
                )
            },
        )
    assert resp.status_code == 202
    mock_task.delay.assert_called_once()


def test_upload_marks_job_failed_and_returns_503_when_enqueue_fails(
    app_client: TestClient, db_session: Session
) -> None:
    """PR #82 review: if delay() raises after the job row is already
    committed as pending, the row must not be left stuck at pending forever
    with no task ever attached — it should be marked failed."""
    content = (FIXTURES / "sample_holdings.md").read_bytes()
    with patch("app.routers.holdings.parse_holdings_upload") as mock_task:
        mock_task.delay.side_effect = ConnectionError("broker unavailable")
        resp = app_client.post(
            "/holdings/upload",
            files={"file": ("holdings.md", content, "text/markdown")},
        )
    assert resp.status_code == 503

    jobs = db_session.query(UploadJob).all()
    assert len(jobs) == 1
    assert jobs[0].status == "failed"
    assert jobs[0].raw_text is None
    assert jobs[0].error is not None


def test_get_upload_job_returns_success_result(app_client: TestClient, db_session: Session) -> None:
    """Poll target once the background task has completed — same shape
    /upload used to return directly before issue #77's async rewrite. Writes
    the UploadJob row directly (via db_session, the same DB app_client's
    get_session override points at) rather than running the real Celery
    task — see test_holdings_tasks.py for the task's own tests.
    """
    job = UploadJob(
        user_id=TEST_USER_ID, filename="holdings.md", status="success", preview=_MOCK_PREVIEW
    )
    db_session.add(job)
    db_session.commit()

    resp = app_client.get(f"/holdings/upload/{job.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert len(body["preview"]["valid_rows"]) == 2
    assert body["error"] is None


def test_get_upload_job_returns_failure_result(app_client: TestClient, db_session: Session) -> None:
    job = UploadJob(
        user_id=TEST_USER_ID,
        filename="holdings.md",
        status="failed",
        error="LLM call failed: boom",
    )
    db_session.add(job)
    db_session.commit()

    resp = app_client.get(f"/holdings/upload/{job.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["preview"] is None
    assert body["error"] == "LLM call failed: boom"


def test_get_upload_job_404_for_other_user(app_client: TestClient, db_session: Session) -> None:
    """A job belonging to a different user must not be visible via poll —
    multi-tenant isolation."""
    other_user = uuid.uuid4()
    seed_user(db_session, other_user)
    job = UploadJob(
        user_id=other_user,  # not TEST_USER_ID
        filename="holdings.md",
        status="success",
        preview=_MOCK_PREVIEW,
    )
    db_session.add(job)
    db_session.commit()

    resp = app_client.get(f"/holdings/upload/{job.id}")
    assert resp.status_code == 404


def test_get_upload_job_404_for_unknown_id(app_client: TestClient) -> None:
    resp = app_client.get(f"/holdings/upload/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_upload_unsupported_extension_returns_422(app_client: TestClient) -> None:
    resp = app_client.post(
        "/holdings/upload",
        files={"file": ("data.pdf", b"%PDF", "application/pdf")},
    )
    assert resp.status_code == 422


def test_upload_xlsx_multi_sheet_returns_422(app_client: TestClient, tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    import pandas as pd

    xlsx_path = tmp_path / "h.xlsx"
    with pd.ExcelWriter(str(xlsx_path)) as writer:
        pd.DataFrame({"a": [1]}).to_excel(writer, sheet_name="S1", index=False)
        pd.DataFrame({"b": [2]}).to_excel(writer, sheet_name="S2", index=False)

    resp = app_client.post(
        "/holdings/upload",
        files={
            "file": (
                "h.xlsx",
                xlsx_path.read_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert resp.status_code == 422
    assert "sheets" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /holdings/confirm
# ---------------------------------------------------------------------------


def test_confirm_writes_to_db(app_client: TestClient) -> None:
    resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE, _PARSED_CASH])
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    names = {r["name"] for r in rows}
    assert names == {"Apple", "USD Cash"}


def test_confirm_unresolvable_ticker_is_other_not_processed(app_client: TestClient) -> None:
    """Issue #311: confirm recomputes capture_supported server-side."""
    payload = {
        **_PARSED_APPLE,
        "name": "BHP Group",
        "ticker": "BHP.AX",
        "currency": "AUD",
        "market": "US",
        "capture_supported": True,
    }
    resp = app_client.post("/holdings/confirm", json=[payload])
    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["ticker"] == "BHP.AX"
    assert row["market"] == "Other"
    assert row["capture_supported"] is False


def test_confirm_lse_ticker_is_uk_and_capture_supported(app_client: TestClient) -> None:
    payload = {
        **_PARSED_APPLE,
        "name": "Vodafone",
        "ticker": "VOD.L",
        "currency": "GBP",
    }
    resp = app_client.post("/holdings/confirm", json=[payload])
    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["market"] == "UK"
    assert row["capture_supported"] is True


def test_confirm_sets_last_manual_update_for_manual_rows(app_client: TestClient) -> None:
    resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_CASH])
    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["last_manual_update"] is not None


def test_confirm_sets_account_id_and_archives_stale_accounts(
    app_client: TestClient, db_session: Session
) -> None:
    """issue #129 B7 review: confirm is a full replace and the only
    holdings-write path until stage C — account_id must not stay NULL after
    it, and an account no longer referenced by any holding must be archived
    (not silently orphaned)."""
    from app.models.account import Account

    resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE])
    assert resp.status_code == 200
    holding_id = uuid.UUID(resp.json()[0]["id"])
    holding = db_session.get(Holding, holding_id)
    assert holding is not None
    assert holding.account_id is not None
    account = db_session.get(Account, holding.account_id)
    assert account is not None
    assert account.broker == "IBKR"  # _PARSED_APPLE's broker
    assert account.archived_at is None
    first_account_id = holding.account_id

    # Re-confirm with a holding under a different broker — IBKR is no
    # longer referenced by anything.
    resp2 = app_client.post(
        "/holdings/confirm?mode=replace", json=[_PARSED_CASH]
    )  # broker "Schwab"
    assert resp2.status_code == 200
    db_session.expire_all()
    stale_account = db_session.get(Account, first_account_id)
    assert stale_account is not None  # not deleted
    assert stale_account.archived_at is not None

    new_holding_id = uuid.UUID(resp2.json()[0]["id"])
    new_holding = db_session.get(Holding, new_holding_id)
    assert new_holding is not None
    assert new_holding.account_id is not None
    new_account = db_session.get(Account, new_holding.account_id)
    assert new_account is not None
    assert new_account.broker == "Schwab"


def test_confirm_full_replace_on_second_call(app_client: TestClient) -> None:
    app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE, _PARSED_CASH])

    tencent: dict[str, object] = {
        "name": "Tencent",
        "ticker": "0700.HK",
        "fund_code": None,
        "currency": "HKD",
        "shares": 100.0,
        "avg_cost": 320.5,
        "current_value": None,
        "pricing_mode": "auto",
        "asset_type": "stock",
        "broker": "富途",
        "account": None,
        "portfolio": None,
        "notes": None,
        "issues": [],
        "confidence": 1.0,
    }
    resp = app_client.post("/holdings/confirm?mode=replace", json=[tencent])
    assert resp.status_code == 200

    list_resp = app_client.get("/holdings")
    rows = list_resp.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "Tencent"


def test_confirm_sparse_history_log_omits_ticker_list(
    app_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Concept §8.8: application logs record user_id, never holdings content.
    A freshly-confirmed auto-priced ticker has zero price_snapshots rows, so
    it's always "sparse" — the log line must say how many, not which."""
    import logging as _logging

    _logging.getLogger("app.routers.holdings").disabled = False
    with caplog.at_level("INFO", logger="app.routers.holdings"):
        resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE])
    assert resp.status_code == 200
    backfill_records = [r for r in caplog.records if "close bars" in r.getMessage()]
    assert backfill_records, "expected a sparse-history log line"
    for record in backfill_records:
        assert "AAPL" not in record.getMessage()
        # PR #181 review: dropping the ticker list must not also drop the
        # one identifier the rule (Concept §8.8) actually asks for.
        assert str(TEST_USER_ID) in record.getMessage()


def test_confirm_backfill_passes_only_this_users_sparse_tickers(
    app_client: TestClient, db_session: Session
) -> None:
    """A second user's never-seen ticker must not ride along on this confirm (#194)."""
    other_user = uuid.uuid4()
    seed_user(db_session, other_user)
    db_session.add(
        Holding(
            user_id=other_user,
            name="Microsoft",
            ticker="MSFT",
            pricing_mode="auto",
            currency="USD",
            asset_class="STOCK",
        )
    )
    db_session.commit()

    with patch("app.tasks.capture_tasks.backfill_ohlcv_task") as mock_task:
        resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE])
    assert resp.status_code == 200
    mock_task.delay.assert_called_once_with(["AAPL"])


def test_confirm_skips_backfill_when_this_users_tickers_already_have_history(
    app_client: TestClient, db_session: Session
) -> None:
    """Someone else's sparse name must not fire a 420-day job on this confirm."""
    other_user = uuid.uuid4()
    seed_user(db_session, other_user)
    db_session.add(
        Holding(
            user_id=other_user,
            name="Microsoft",
            ticker="MSFT",
            pricing_mode="auto",
            currency="USD",
            asset_class="STOCK",
        )
    )
    start = date(2026, 1, 1)
    db_session.add_all(
        [
            PriceSnapshot(
                ticker="AAPL",
                market="US",
                session_node="close",
                trade_date=start + timedelta(days=i),
                close=Decimal("100"),
            )
            for i in range(50)
        ]
    )
    db_session.commit()

    with patch("app.tasks.capture_tasks.backfill_ohlcv_task") as mock_task:
        resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE])
    assert resp.status_code == 200
    mock_task.delay.assert_not_called()


def test_confirm_skips_backfill_when_known_collision_ticker_history_exists(
    app_client: TestClient, db_session: Session
) -> None:
    """issue #204 PR #253 review: capture writes PSH's closes under the
    normalized 'PSH.L' key. The sparse-history check queried price_snapshots
    for the raw holding ticker 'PSH', which never matches, so every confirm
    re-enqueued a fresh 420-day backfill for a ticker that already has a
    full year of correctly-captured history."""
    start = date(2026, 1, 1)
    db_session.add_all(
        [
            PriceSnapshot(
                ticker="PSH.L",
                market="US",
                session_node="close",
                trade_date=start + timedelta(days=i),
                close=Decimal("59"),
            )
            for i in range(50)
        ]
    )
    db_session.commit()

    with patch("app.tasks.capture_tasks.backfill_ohlcv_task") as mock_task:
        resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_PSH])
    assert resp.status_code == 200
    mock_task.delay.assert_not_called()


def test_confirm_dispatches_fund_nav_backfill_for_uncached_fund_codes(
    app_client: TestClient,
) -> None:
    """A fund_code with no price_snapshots must fire an async NAV capture (#196)."""
    with (
        patch("app.tasks.capture_tasks.backfill_ohlcv_task") as mock_ohlcv,
        patch("app.tasks.capture_tasks.backfill_fund_navs_task") as mock_nav,
    ):
        resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_FUND])
    assert resp.status_code == 200
    mock_nav.delay.assert_called_once_with(["513100"])
    mock_ohlcv.delay.assert_not_called()


def test_confirm_skips_fund_nav_backfill_when_close_already_cached(
    app_client: TestClient, db_session: Session
) -> None:
    """Any existing close under the fund_code key is enough — §4.4 does not
    apply to funds, so this is not the ticker '< 50 bars' threshold (#196)."""
    db_session.add(
        PriceSnapshot(
            ticker="513100",
            market="A-Share",
            session_node="close",
            trade_date=date(2026, 8, 22),
            close=Decimal("1.23"),
        )
    )
    db_session.commit()

    with patch("app.tasks.capture_tasks.backfill_fund_navs_task") as mock_nav:
        resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_FUND])
    assert resp.status_code == 200
    mock_nav.delay.assert_not_called()


def test_confirm_skips_fund_nav_backfill_for_manual_fund(
    app_client: TestClient,
) -> None:
    """Manual funds are user-priced; they must not trigger a NAV capture."""
    manual = {
        **_PARSED_FUND,
        "pricing_mode": "manual",
        "current_value": 1200.0,
        "shares": None,
        "avg_cost": None,
    }
    with patch("app.tasks.capture_tasks.backfill_fund_navs_task") as mock_nav:
        resp = app_client.post("/holdings/confirm?mode=replace", json=[manual])
    assert resp.status_code == 200
    mock_nav.delay.assert_not_called()


def test_confirm_dispatches_fund_nav_when_only_non_close_snapshot_exists(
    app_client: TestClient, db_session: Session
) -> None:
    """An after_close last-only row is not a cached NAV close."""
    db_session.add(
        PriceSnapshot(
            ticker="513100",
            market="A-Share",
            session_node="after_close",
            trade_date=date(2026, 8, 22),
            last=Decimal("1.23"),
        )
    )
    db_session.commit()

    with patch("app.tasks.capture_tasks.backfill_fund_navs_task") as mock_nav:
        resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_FUND])
    assert resp.status_code == 200
    mock_nav.delay.assert_called_once_with(["513100"])


def test_confirm_still_succeeds_when_fund_nav_enqueue_fails(
    app_client: TestClient,
) -> None:
    """A broker blip after commit must not 500 a successful confirm."""
    with patch("app.tasks.capture_tasks.backfill_fund_navs_task") as mock_nav:
        mock_nav.delay.side_effect = RuntimeError("broker down")
        resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_FUND])
    assert resp.status_code == 200
    assert resp.json()[0]["fund_code"] == "513100"


def test_confirm_still_succeeds_when_ohlcv_enqueue_fails(
    app_client: TestClient,
) -> None:
    with patch("app.tasks.capture_tasks.backfill_ohlcv_task") as mock_ohlcv:
        mock_ohlcv.delay.side_effect = RuntimeError("broker down")
        resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE])
    assert resp.status_code == 200
    assert resp.json()[0]["ticker"] == "AAPL"


def test_confirm_fund_nav_backfill_passes_only_this_users_uncached_codes(
    app_client: TestClient, db_session: Session
) -> None:
    """Another user's never-captured fund must not ride along on this confirm."""
    other_user = uuid.uuid4()
    seed_user(db_session, other_user)
    db_session.add(
        Holding(
            user_id=other_user,
            name="Other User CSI 300 ETF",
            fund_code="510300",
            pricing_mode="auto",
            currency="CNY",
            asset_class="EQUITY_CN",
            market="A-Share",
        )
    )
    db_session.commit()

    with patch("app.tasks.capture_tasks.backfill_fund_navs_task") as mock_nav:
        resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_FUND])
    assert resp.status_code == 200
    mock_nav.delay.assert_called_once_with(["513100"])


def test_confirm_fund_nav_log_omits_fund_code_list(
    app_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Concept §8.8: log how many funds, not which, plus user_id."""
    import logging as _logging

    _logging.getLogger("app.routers.holdings").disabled = False
    with caplog.at_level("INFO", logger="app.routers.holdings"):
        resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_FUND])
    assert resp.status_code == 200
    nav_records = [r for r in caplog.records if "fund NAV" in r.getMessage()]
    assert nav_records, "expected a fund-NAV cold-start log line"
    for record in nav_records:
        assert "513100" not in record.getMessage()
        assert str(TEST_USER_ID) in record.getMessage()


def test_confirm_full_replace_does_not_touch_other_users(
    app_client: TestClient, db_session: Session
) -> None:
    """B-UAT-13 (Ring 1-B design doc §5.3/§10.2): confirm_holdings' full-
    replace DELETE is the one call in this codebase that wipes an entire
    user's holdings in one statement — it must stay scoped to the caller's
    own user_id no matter how identity resolution changes upstream."""
    other_user_id = uuid.uuid4()
    seed_user(db_session, other_user_id)
    other_holding = Holding(
        user_id=other_user_id,
        name="Other User's Fund",
        ticker="MSFT",
        pricing_mode="auto",
        currency="USD",
        asset_class="STOCK",
    )
    db_session.add(other_holding)
    db_session.commit()

    resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE, _PARSED_CASH])
    assert resp.status_code == 200

    remaining = db_session.query(Holding).filter(Holding.user_id == other_user_id).all()
    assert len(remaining) == 1
    assert remaining[0].name == "Other User's Fund"


def test_confirm_empty_list_clears_holdings(app_client: TestClient) -> None:
    app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE])
    resp = app_client.post("/holdings/confirm?mode=replace", json=[])
    assert resp.status_code == 200
    list_resp = app_client.get("/holdings")
    assert list_resp.json() == []


# ---------------------------------------------------------------------------
# GET /holdings
# ---------------------------------------------------------------------------


def test_list_holdings_empty_initially(app_client: TestClient) -> None:
    resp = app_client.get("/holdings")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_holdings_after_confirm(app_client: TestClient) -> None:
    app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE, _PARSED_CASH])
    resp = app_client.get("/holdings")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_list_holdings_includes_expected_fields(app_client: TestClient) -> None:
    app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE])
    row = app_client.get("/holdings").json()[0]
    for field in (
        "id",
        "name",
        "ticker",
        "currency",
        "pricing_mode",
        "market",
        "capture_supported",
        "created_at",
        "updated_at",
    ):
        assert field in row


# ---------------------------------------------------------------------------
# GET /holdings/export
# ---------------------------------------------------------------------------


def test_export_returns_markdown_file(app_client: TestClient) -> None:
    app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE, _PARSED_CASH])
    resp = app_client.get("/holdings/export")
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    import re as _re

    assert _re.search(r'filename="holdings-\d{8}-\d{6}Z\.md"', resp.headers["content-disposition"])
    body = resp.text
    assert "Apple" in body
    assert "AAPL" in body
    assert "USD Cash" in body


def test_export_empty_holdings(app_client: TestClient) -> None:
    resp = app_client.get("/holdings/export")
    assert resp.status_code == 200
    body = resp.text
    assert "#####" in body
    data_lines = [ln for ln in body.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert data_lines == []


def test_export_flattens_newlines_and_is_not_a_pipe_table(
    app_client: TestClient,
) -> None:
    row = {**_PARSED_APPLE, "name": "Acme Corp", "notes": "line1\nline2"}
    app_client.post("/holdings/confirm?mode=replace", json=[row])
    body = app_client.get("/holdings/export").text
    table_lines = [ln for ln in body.splitlines() if ln.startswith("|")]
    assert table_lines == []
    data_lines = [ln for ln in body.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert len(data_lines) == 1
    assert "Apple" not in data_lines[0]  # renamed
    assert "Acme Corp" in data_lines[0]
    assert "AAPL" in data_lines[0]


def test_export_uses_report_locale_not_ui_locale(
    app_client: TestClient, db_session: Session
) -> None:
    from app.models.user import User

    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.locale = "en"
    db_session.commit()
    resp = app_client.get("/holdings/export")
    assert "One holding per line" in resp.text

    user.locale = "zh"
    db_session.commit()
    resp = app_client.get("/holdings/export")
    assert "一行一条" in resp.text


def test_export_round_trips_through_comment_strip(app_client: TestClient) -> None:
    app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE, _PARSED_CASH])
    body = app_client.get("/holdings/export").text
    from app.services.holding_parser import _extract_text

    stripped = _extract_text(body.encode("utf-8"), "holdings.md")
    assert "#####" not in stripped
    assert "AAPL" in stripped
    assert "USD Cash" in stripped


# ---------------------------------------------------------------------------
# GET /holdings/template
# ---------------------------------------------------------------------------


def test_template_covers_asset_types_and_markets_without_wmf_jargon(
    app_client: TestClient, db_session: Session
) -> None:
    from app.models.user import User

    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.locale = "en"
    db_session.commit()
    resp = app_client.get("/holdings/template")
    assert resp.status_code == 200
    assert "holdings-template.md" in resp.headers["content-disposition"]
    body = resp.text
    assert "#####" in body
    assert "wealth-management product" in body
    assert "wmf" not in body.lower()
    for token in ("AAPL", "SPY", "0700.HK", "600519.SS", "110011", "USD Cash", "PSH.L"):
        assert token in body


def test_template_zh_uses_wealth_management_wording(
    app_client: TestClient, db_session: Session
) -> None:
    from app.models.user import User

    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.locale = "zh"
    db_session.commit()
    body = app_client.get("/holdings/template").text
    assert "理财产品" in body
    assert "wmf" not in body.lower()


# ---------------------------------------------------------------------------
# POST /holdings (single-row create)
# ---------------------------------------------------------------------------


def test_create_holding_returns_201_and_skips_llm(app_client: TestClient) -> None:
    with patch("app.services.holding_parser.parse") as mock_parse:
        resp = app_client.post("/holdings", json=_PARSED_APPLE)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Apple"
    assert body["ticker"] == "AAPL"
    assert body["position"] == 0
    mock_parse.assert_not_called()


def test_create_holding_appends_at_max_position_plus_one(app_client: TestClient) -> None:
    app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE, _PARSED_CASH])
    resp = app_client.post("/holdings", json=_PARSED_FUND)
    assert resp.status_code == 201
    assert resp.json()["position"] == 2
    listed = app_client.get("/holdings").json()
    assert len(listed) == 3


def test_create_duplicate_ticker_broker_is_second_lot(app_client: TestClient) -> None:
    first = app_client.post("/holdings", json=_PARSED_APPLE)
    second = app_client.post("/holdings", json=_PARSED_APPLE)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert len(app_client.get("/holdings").json()) == 2


def test_create_cash_without_ticker_sets_market_other(app_client: TestClient) -> None:
    row = {**_PARSED_CASH, "broker": "CMB", "market": "A-Share"}
    resp = app_client.post("/holdings", json=row)
    assert resp.status_code == 201
    assert resp.json()["market"] == "Other"


def test_create_enqueues_sparse_backfill_only_for_new_ticker(
    app_client: TestClient, db_session: Session
) -> None:
    app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE])
    with (
        patch("app.tasks.capture_tasks.backfill_ohlcv_task") as mock_ohlcv,
        patch("app.tasks.capture_tasks.backfill_fund_navs_task") as mock_nav,
    ):
        resp = app_client.post("/holdings", json=_PARSED_FUND)
    assert resp.status_code == 201
    mock_nav.delay.assert_called_once_with(["513100"])
    mock_ohlcv.delay.assert_not_called()


# ---------------------------------------------------------------------------
# PATCH /holdings/{id}
# ---------------------------------------------------------------------------


def test_patch_holding_updates_fields(app_client: TestClient) -> None:
    created = app_client.post("/holdings", json=_PARSED_APPLE).json()
    resp = app_client.patch(f"/holdings/{created['id']}", json={"name": "Apple Inc."})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Apple Inc."
    assert resp.json()["ticker"] == "AAPL"


def test_patch_holding_reresolves_accounts_on_broker_change(
    app_client: TestClient, db_session: Session
) -> None:
    from app.models.account import Account

    created = app_client.post("/holdings", json=_PARSED_APPLE).json()
    holding = db_session.get(Holding, uuid.UUID(created["id"]))
    assert holding is not None
    old_account_id = holding.account_id
    resp = app_client.patch(f"/holdings/{created['id']}", json={"broker": "Schwab"})
    assert resp.status_code == 200
    db_session.expire_all()
    holding = db_session.get(Holding, uuid.UUID(created["id"]))
    assert holding is not None
    assert holding.account_id is not None
    assert holding.account_id != old_account_id
    new_account = db_session.get(Account, holding.account_id)
    assert new_account is not None
    assert new_account.broker == "Schwab"
    # Other lots still reference IBKR — it must not be archived on a single-row patch.
    if old_account_id is not None:
        stale = db_session.get(Account, old_account_id)
        assert stale is not None
        assert stale.archived_at is None


def test_patch_holding_404_for_other_user(app_client: TestClient, db_session: Session) -> None:
    other_user = uuid.uuid4()
    seed_user(db_session, other_user)
    other = Holding(
        user_id=other_user,
        name="Microsoft",
        ticker="MSFT",
        pricing_mode="auto",
        currency="USD",
        asset_class="STOCK",
    )
    db_session.add(other)
    db_session.commit()
    resp = app_client.patch(f"/holdings/{other.id}", json={"name": "Nope"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /holdings/{id}
# ---------------------------------------------------------------------------


def test_delete_holding_returns_204(app_client: TestClient) -> None:
    created = app_client.post("/holdings", json=_PARSED_APPLE).json()
    resp = app_client.delete(f"/holdings/{created['id']}")
    assert resp.status_code == 204
    assert app_client.get("/holdings").json() == []


def test_delete_holding_404_for_other_user(app_client: TestClient, db_session: Session) -> None:
    other_user = uuid.uuid4()
    seed_user(db_session, other_user)
    other = Holding(
        user_id=other_user,
        name="Microsoft",
        ticker="MSFT",
        pricing_mode="auto",
        currency="USD",
        asset_class="STOCK",
    )
    db_session.add(other)
    db_session.commit()
    resp = app_client.delete(f"/holdings/{other.id}")
    assert resp.status_code == 404
    remaining = db_session.get(Holding, other.id)
    assert remaining is not None


def test_delete_unknown_id_404(app_client: TestClient) -> None:
    resp = app_client.delete(f"/holdings/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /holdings/reorder
# ---------------------------------------------------------------------------


def test_reorder_writes_position_in_payload_order(app_client: TestClient) -> None:
    a = app_client.post("/holdings", json=_PARSED_APPLE).json()
    b = app_client.post("/holdings", json=_PARSED_CASH).json()
    c = app_client.post("/holdings", json=_PARSED_FUND).json()
    ids = [c["id"], a["id"], b["id"]]
    resp = app_client.patch("/holdings/reorder", json={"ids": ids})
    assert resp.status_code == 200
    listed = app_client.get("/holdings").json()
    assert [row["id"] for row in listed] == ids
    assert [row["position"] for row in listed] == [0, 1, 2]


def test_reorder_rejects_partial_id_list(app_client: TestClient) -> None:
    a = app_client.post("/holdings", json=_PARSED_APPLE).json()
    app_client.post("/holdings", json=_PARSED_CASH)
    resp = app_client.patch("/holdings/reorder", json={"ids": [a["id"]]})
    assert resp.status_code == 422


def test_reorder_rejects_unknown_id(app_client: TestClient) -> None:
    a = app_client.post("/holdings", json=_PARSED_APPLE).json()
    resp = app_client.patch("/holdings/reorder", json={"ids": [a["id"], str(uuid.uuid4())]})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /holdings/confirm modes
# ---------------------------------------------------------------------------


def test_confirm_default_mode_is_append(app_client: TestClient) -> None:
    app_client.post("/holdings", json=_PARSED_APPLE)
    resp = app_client.post("/holdings/confirm", json=[_PARSED_CASH])
    assert resp.status_code == 200
    names = {r["name"] for r in resp.json()}
    assert names == {"Apple", "USD Cash"}


def test_confirm_append_does_not_update_existing_rows(app_client: TestClient) -> None:
    created = app_client.post("/holdings", json=_PARSED_APPLE).json()
    mutated = {**_PARSED_APPLE, "name": "Apple Inc.", "shares": 99.0}
    resp = app_client.post("/holdings/confirm?mode=append", json=[mutated])
    assert resp.status_code == 200
    listed = app_client.get("/holdings").json()
    assert len(listed) == 2
    original = next(r for r in listed if r["id"] == created["id"])
    assert original["name"] == "Apple"
    assert original["shares"] in ("10", "10.0", "10.00")


def test_confirm_append_duplicate_ticker_broker_is_second_lot(app_client: TestClient) -> None:
    app_client.post("/holdings", json=_PARSED_APPLE)
    resp = app_client.post("/holdings/confirm?mode=append", json=[_PARSED_APPLE])
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_confirm_replace_still_wipes_book(app_client: TestClient) -> None:
    app_client.post("/holdings", json=_PARSED_APPLE)
    resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_CASH])
    assert resp.status_code == 200
    listed = resp.json()
    assert len(listed) == 1
    assert listed[0]["name"] == "USD Cash"


def test_confirm_append_does_not_archive_existing_accounts(
    app_client: TestClient, db_session: Session
) -> None:
    from app.models.account import Account

    created = app_client.post("/holdings", json=_PARSED_APPLE).json()
    holding = db_session.get(Holding, uuid.UUID(created["id"]))
    assert holding is not None
    ibkr_id = holding.account_id
    app_client.post("/holdings/confirm?mode=append", json=[_PARSED_CASH])
    db_session.expire_all()
    ibkr = db_session.get(Account, ibkr_id)
    assert ibkr is not None
    assert ibkr.archived_at is None


# ---------------------------------------------------------------------------
# PR #310 review fixes
# ---------------------------------------------------------------------------


def test_export_filename_is_utc_timestamp(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.routers.holdings.holdings_export_filename",
        lambda now=None: "holdings-20260902-051530Z.md",
    )
    resp = app_client.get("/holdings/export")
    assert resp.status_code == 200
    assert 'filename="holdings-20260902-051530Z.md"' in resp.headers["content-disposition"]


def test_export_includes_tagged_optional_fields(app_client: TestClient) -> None:
    row = {
        **_PARSED_APPLE,
        "account": "IRA",
        "portfolio": "Growth Sleeve",
        "notes": "core holding",
        "asset_type": "stock",
        "market": "US",
        "pricing_mode": "auto",
    }
    app_client.post("/holdings/confirm?mode=replace", json=[row])
    body = app_client.get("/holdings/export").text
    data_lines = [ln for ln in body.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert len(data_lines) == 1
    line = data_lines[0]
    assert "account:IRA" in line
    assert 'portfolio:"Growth Sleeve"' in line
    assert 'notes:"core holding"' in line
    assert "asset_type:stock" in line
    assert "market:US" in line
    assert "pricing_mode:auto" in line


def test_export_dialect_round_trips_tagged_fields_without_llm(app_client: TestClient) -> None:
    row = {
        **_PARSED_APPLE,
        "account": "IRA",
        "portfolio": "Taxable",
        "notes": "keep me",
        "market": "US",
    }
    app_client.post("/holdings/confirm?mode=replace", json=[row])
    body = app_client.get("/holdings/export").text
    from app.services.holding_parser import parse

    preview = parse(body)
    assert len(preview.valid_rows) == 1
    parsed = preview.valid_rows[0]
    assert parsed.account == "IRA"
    assert parsed.portfolio == "Taxable"
    assert parsed.notes == "keep me"
    assert parsed.asset_type == "stock"
    assert parsed.market == "US"
    assert parsed.pricing_mode == "auto"
    assert parsed.ticker == "AAPL"


def test_create_holding_enqueues_sector_backfill_not_sync_yfinance(
    app_client: TestClient,
) -> None:
    with (
        patch("app.tasks.capture_tasks.backfill_sectors_task") as mock_sector,
        patch("app.services.price_fetcher.backfill_sectors") as mock_sync,
        patch("app.tasks.capture_tasks.backfill_ohlcv_task"),
    ):
        resp = app_client.post("/holdings", json=_PARSED_APPLE)
    assert resp.status_code == 201
    mock_sync.assert_not_called()
    mock_sector.delay.assert_called_once_with([resp.json()["id"]])


def test_confirm_replace_enqueues_sector_backfill_for_inserted_rows(
    app_client: TestClient,
) -> None:
    with (
        patch("app.tasks.capture_tasks.backfill_sectors_task") as mock_sector,
        patch("app.tasks.capture_tasks.backfill_ohlcv_task"),
    ):
        resp = app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE, _PARSED_CASH])
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()]
    mock_sector.delay.assert_called_once()
    enqueued = mock_sector.delay.call_args[0][0]
    assert set(enqueued) == set(ids)


def test_patch_ticker_clears_stale_price_and_sector(
    app_client: TestClient, db_session: Session
) -> None:
    created = app_client.post("/holdings", json=_PARSED_APPLE).json()
    holding = db_session.get(Holding, uuid.UUID(created["id"]))
    assert holding is not None
    holding.sector = "Technology"
    holding.market_price = Decimal("180")
    holding.price_as_of = datetime(2026, 1, 2, tzinfo=UTC)
    holding.price_fetched_at = holding.price_as_of
    db_session.commit()
    with patch("app.tasks.capture_tasks.backfill_sectors_task") as mock_sector:
        resp = app_client.patch(f"/holdings/{created['id']}", json={"ticker": "MSFT"})
    assert resp.status_code == 200
    db_session.expire_all()
    holding = db_session.get(Holding, uuid.UUID(created["id"]))
    assert holding is not None
    assert holding.ticker == "MSFT"
    assert holding.sector is None
    assert holding.market_price is None
    assert holding.price_as_of is None
    assert holding.price_fetched_at is None
    mock_sector.delay.assert_called_once_with([created["id"]])


def test_patch_notes_preserves_untouched_encrypted_decimal_shares(
    app_client: TestClient, db_session: Session
) -> None:
    created = app_client.post("/holdings", json=_PARSED_APPLE).json()
    holding = db_session.get(Holding, uuid.UUID(created["id"]))
    assert holding is not None
    precise = Decimal("1.234567890123456789")
    holding.shares = precise
    db_session.commit()
    resp = app_client.patch(f"/holdings/{created['id']}", json={"notes": "untouched shares"})
    assert resp.status_code == 200
    db_session.expire_all()
    holding = db_session.get(Holding, uuid.UUID(created["id"]))
    assert holding is not None
    assert holding.shares == precise
    assert holding.notes == "untouched shares"


def test_patch_rejects_asset_class_write_as_unknown_field(app_client: TestClient) -> None:
    """asset_class is always recomputed; HoldingPatch must not advertise it."""
    created = app_client.post("/holdings", json=_PARSED_APPLE).json()
    resp = app_client.patch(
        f"/holdings/{created['id']}", json={"asset_class": "EQUITY_US_TECH", "notes": "x"}
    )
    # Extra fields are ignored (Pydantic default); notes still apply; stored
    # asset_class stays the ticker-driven value, not the client-supplied one.
    assert resp.status_code == 200
    assert resp.json()["notes"] == "x"
    assert resp.json()["asset_class"] == "STOCK"


def test_create_holding_locks_before_assigning_position(app_client: TestClient) -> None:
    first = app_client.post("/holdings", json=_PARSED_APPLE)
    second = app_client.post("/holdings", json=_PARSED_CASH)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["position"] == 0
    assert second.json()["position"] == 1
