"""Integration tests for /holdings endpoints — real Postgres, mocked LLM."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.models.upload_job import UploadJob
from app.services.holdings_export import MANUAL_LISTED_PLACEHOLDER
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


def test_export_uses_report_locale_when_no_locale_param_given(
    app_client: TestClient, db_session: Session
) -> None:
    """issue #319 item 9: `_report_locale` (users.locale) is the fallback
    when the caller omits the `locale` query param — unchanged from before
    this batch."""
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


def test_export_locale_param_takes_precedence_over_report_locale(
    app_client: TestClient, db_session: Session
) -> None:
    """issue #319 item 9: an explicit `locale` query param (the frontend's
    UI locale) overrides users.locale (report language) — the two are
    independently controllable."""
    from app.models.user import User

    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.locale = "zh"
    db_session.commit()

    resp = app_client.get("/holdings/export?locale=en")
    assert "One holding per line" in resp.text
    assert "一行一条" not in resp.text

    user.locale = "en"
    db_session.commit()
    resp = app_client.get("/holdings/export?locale=zh")
    assert "一行一条" in resp.text


def test_export_locale_param_unrecognized_value_falls_back_to_english(
    app_client: TestClient,
) -> None:
    """A locale render_rules does not recognize (e.g. zh-Hant, still gated
    out of the frontend switcher) falls back to English rather than 404ing
    or erroring — this endpoint does not itself validate the value."""
    resp = app_client.get("/holdings/export?locale=zh-Hant")
    assert resp.status_code == 200
    assert "One holding per line" in resp.text


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
    for token in (
        "AAPL",
        "SPY",
        "0700.HK",
        "600519.SS",
        "110011",
        "USD Cash",
        "Pershing Square PSH.L GBP",
    ):
        assert token in body
    assert "Do not write a .L suffix" not in body
    assert "Do not add an exchange suffix" not in body


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


def test_template_locale_param_takes_precedence_over_report_locale(
    app_client: TestClient, db_session: Session
) -> None:
    """issue #319 item 9, template's sibling of the export test above."""
    from app.models.user import User

    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.locale = "en"
    db_session.commit()

    resp = app_client.get("/holdings/template?locale=zh")
    assert "理财产品" in resp.text


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


def test_export_omits_asset_type_market_pricing_mode_tags(app_client: TestClient) -> None:
    """issue #319 item 8: `asset_type:`/`market:`/`pricing_mode:` no longer
    appear in export output — pure classification, always re-derivable via
    the LLM path. `account:`/`portfolio:`/`notes:` are NOT part of this —
    see the next test — they are free-text user data with no other slot
    in the positional dialect (PR #321 review round 1, blacktomb42: the
    first version of this change dropped all six keys, an unrecoverable
    data-loss regression the review caught before merge)."""
    row = {
        **_PARSED_APPLE,
        "asset_type": "stock",
        "market": "US",
        "pricing_mode": "auto",
    }
    app_client.post("/holdings/confirm?mode=replace", json=[row])
    body = app_client.get("/holdings/export").text
    data_lines = [ln for ln in body.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert len(data_lines) == 1
    line = data_lines[0]
    assert "Apple" in line
    assert "AAPL" in line
    for tag in ("asset_type:", "market:", "pricing_mode:"):
        assert tag not in line


def test_export_keeps_account_portfolio_notes_tags(app_client: TestClient) -> None:
    """account/portfolio/notes are free-text and have no positional slot —
    unlike asset_type/market/pricing_mode, dropping them would be
    unrecoverable, not merely "no longer free/fast", so export keeps
    emitting them."""
    row = {
        **_PARSED_APPLE,
        "account": "IRA",
        "portfolio": "Growth Sleeve",
        "notes": "core holding",
    }
    app_client.post("/holdings/confirm?mode=replace", json=[row])
    body = app_client.get("/holdings/export").text
    data_lines = [ln for ln in body.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert len(data_lines) == 1
    line = data_lines[0]
    assert "account:IRA" in line
    assert 'portfolio:"Growth Sleeve"' in line
    assert 'notes:"core holding"' in line


def test_export_plain_row_no_longer_triggers_the_dialect_fast_path(
    app_client: TestClient,
) -> None:
    """The explicit tradeoff behind item 8: a row with no account/
    portfolio/notes now exports with zero trailing tags (pricing_mode is
    deliberately excluded from export precisely because it's the one tag
    every Holding always has — see the module docstring), so
    `try_parse_dialect` no longer matches a typical export. Asserted
    directly against `try_parse_dialect` (not a full `parse()` round
    trip) so this test needs no LLM mock."""
    app_client.post("/holdings/confirm?mode=replace", json=[_PARSED_APPLE])
    body = app_client.get("/holdings/export").text
    from app.services.holding_parser import try_parse_dialect

    assert try_parse_dialect(body) is None


def test_export_manual_row_with_surviving_tag_still_round_trips_via_dialect_path(
    app_client: TestClient,
) -> None:
    """Regression lock for the bug PR #321 review round 1 caught in the
    fix itself: a manual-pricing row that also has account/portfolio/
    notes set still carries a tag (so the file *does* hit the dialect
    fast path via that tag), and must still route to the manual 3-slot
    parser rather than being silently misparsed as 2-slot auto — which
    would swallow current_value into the broker field and flip
    pricing_mode back to auto. `_manual_match_explicit` in
    `holding_parser.py` is tried positionally before any tag check
    specifically to guarantee this."""
    row: dict[str, object] = {
        "name": "Family house",
        "ticker": "HOME",
        "fund_code": None,
        "currency": "USD",
        "shares": 1.0,
        "avg_cost": 240000.0,
        "current_value": 250000.0,
        "pricing_mode": "manual",
        "asset_type": "other",
        "broker": "IBKR",
        "account": "Taxable",
        "portfolio": None,
        "notes": None,
        "issues": [],
        "confidence": 1.0,
    }
    resp = app_client.post("/holdings", json=row)
    assert resp.status_code == 201
    body = app_client.get("/holdings/export").text
    from app.services.holding_parser import try_parse_dialect

    dialect_rows = try_parse_dialect(body)
    assert dialect_rows is not None, "expected the account: tag to trigger the dialect path"
    assert len(dialect_rows) == 1
    parsed = dialect_rows[0]
    assert parsed["shares"] == 1.0
    assert parsed["avg_cost"] == 240000.0
    assert parsed["current_value"] == 250000.0
    assert parsed["pricing_mode"] == "manual"
    assert parsed["broker"] == "IBKR"


def test_export_auto_listed_row_with_surviving_tag_still_round_trips_via_dialect_path(
    app_client: TestClient,
) -> None:
    """PR #321 review round 3 suggestion: lock the other branch of round
    1's reordering — an ordinary auto (2-slot) listed row that also
    carries a surviving account/notes tag must still route to
    `_parse_listed_tokens`, not get misdetected as the manual 3-slot
    shape by the now-first `_manual_match_explicit` check."""
    row = {**_PARSED_APPLE, "account": "IRA", "notes": "core holding"}
    app_client.post("/holdings/confirm?mode=replace", json=[row])
    body = app_client.get("/holdings/export").text
    from app.services.holding_parser import try_parse_dialect

    dialect_rows = try_parse_dialect(body)
    assert dialect_rows is not None, "expected the account: tag to trigger the dialect path"
    assert len(dialect_rows) == 1
    parsed = dialect_rows[0]
    assert parsed["ticker"] == "AAPL"
    assert parsed["shares"] == 10.0
    assert parsed["avg_cost"] == 180.0
    assert parsed["broker"] == "IBKR"
    assert parsed["pricing_mode"] == "auto"


def test_export_cash_row_with_surviving_tag_still_round_trips_via_dialect_path(
    app_client: TestClient,
) -> None:
    """PR #321 review round 3 suggestion: a cash/wmf row's export line no
    longer carries an asset_type tag (item 8), so `parse_dialect_line`
    only reaches `_parse_cash_tokens` via its no-ticker/no-fund-code
    fallback — lock that a surviving account/notes tag doesn't divert it
    into `_parse_listed_tokens` or `_manual_match_explicit` instead."""
    row = {**_PARSED_CASH, "account": "Checking", "notes": "emergency fund"}
    app_client.post("/holdings/confirm?mode=replace", json=[row])
    body = app_client.get("/holdings/export").text
    from app.services.holding_parser import try_parse_dialect

    dialect_rows = try_parse_dialect(body)
    assert dialect_rows is not None, "expected the account: tag to trigger the dialect path"
    assert len(dialect_rows) == 1
    parsed = dialect_rows[0]
    assert parsed["current_value"] == 15000.0
    assert parsed.get("ticker") is None
    assert parsed["pricing_mode"] == "manual"


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
    mock_sector.delay.assert_called_once_with([resp.json()["id"]], str(TEST_USER_ID))


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
    assert mock_sector.delay.call_args[0][1] == str(TEST_USER_ID)


def test_patch_holding_locks_the_row_before_reading_it(app_client: TestClient) -> None:
    """PR #321 review round 3: update_holding's read-modify-write was
    unlocked while create/confirm/reorder all lock — two overlapping
    single-field PATCHes on the same row (an ordinary interaction once
    #319 made inline edit real) could each merge against a stale
    pre-edit row and the later commit could silently drop the earlier
    field for any non-money column. `wraps` keeps the real lookup/lock
    behavior so the request still succeeds normally; this only asserts
    the call carried `for_update=True` — mirrors the existing
    test_reorder_locks_before_assigning_position lock-call assertion
    pattern rather than simulating true concurrency."""
    from app.routers import holdings as holdings_router

    created = app_client.post("/holdings", json=_PARSED_APPLE).json()
    with patch("app.routers.holdings._own_holding", wraps=holdings_router._own_holding) as mock_own:
        resp = app_client.patch(f"/holdings/{created['id']}", json={"notes": "x"})
    assert resp.status_code == 200
    assert resp.json()["notes"] == "x"
    mock_own.assert_called_once_with(ANY, TEST_USER_ID, uuid.UUID(created["id"]), for_update=True)


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
    mock_sector.delay.assert_called_once_with([created["id"]], str(TEST_USER_ID))


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


def test_export_manual_non_cash_emits_shares_avg_cost_and_current_value(
    app_client: TestClient,
) -> None:
    """Manual non-cash must export shares+avg_cost+current_value so cost
    basis survives — unaffected by item 8 (that removed the trailing tag
    segment, not this manual-listed positional emission). No round trip
    through `parse()` here any more: without a tag, `try_parse_dialect`
    never fires (see test_export_no_longer_triggers_the_dialect_fast_path
    above), so this is a pure export-format assertion."""
    row: dict[str, object] = {
        "name": "Family house",
        "ticker": "HOME",
        "fund_code": None,
        "currency": "USD",
        "shares": 1.0,
        "avg_cost": 240000.0,
        "current_value": 250000.0,
        "pricing_mode": "manual",
        "asset_type": "other",
        "broker": "IBKR",
        "account": None,
        "portfolio": None,
        "notes": None,
        "issues": [],
        "confidence": 1.0,
    }
    resp = app_client.post("/holdings", json=row)
    assert resp.status_code == 201
    body = app_client.get("/holdings/export").text
    data_lines = [ln for ln in body.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert len(data_lines) == 1
    line = data_lines[0]
    assert "HOME" in line
    assert "1" in line
    assert "240000" in line
    assert "250000" in line
    assert "pricing_mode:" not in line


def test_export_manual_non_cash_missing_avg_cost_uses_placeholder_not_fabricated(
    app_client: TestClient,
) -> None:
    """PR #310 round 5: shares + current_value known, avg_cost unknown must
    still be placeholder-marked (`-`), not omitted — an omitted slot is
    unrecoverable to a reader/parser trying to tell it apart from 'shares +
    avg_cost, no current_value' by position count alone."""
    row: dict[str, object] = {
        "name": "Family house",
        "ticker": "HOME",
        "fund_code": None,
        "currency": "USD",
        "shares": 1.0,
        "avg_cost": None,
        "current_value": 250000.0,
        "pricing_mode": "manual",
        "asset_type": "other",
        "broker": "IBKR",
        "account": None,
        "portfolio": None,
        "notes": None,
        "issues": [],
        "confidence": 1.0,
    }
    resp = app_client.post("/holdings", json=row)
    assert resp.status_code == 201
    body = app_client.get("/holdings/export").text
    data_lines = [ln for ln in body.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert len(data_lines) == 1
    tokens = data_lines[0].split()
    assert MANUAL_LISTED_PLACEHOLDER in tokens


def test_export_manual_non_cash_missing_shares_uses_placeholder_not_fabricated(
    app_client: TestClient,
) -> None:
    """The inverse gap: avg_cost + current_value known, shares unknown."""
    row: dict[str, object] = {
        "name": "Family office stake",
        "ticker": "HOME",
        "fund_code": None,
        "currency": "USD",
        "shares": None,
        "avg_cost": 25000.0,
        "current_value": 280000.0,
        "pricing_mode": "manual",
        "asset_type": "other",
        "broker": "IBKR",
        "account": None,
        "portfolio": None,
        "notes": None,
        "issues": [],
        "confidence": 1.0,
    }
    resp = app_client.post("/holdings", json=row)
    assert resp.status_code == 201
    body = app_client.get("/holdings/export").text
    data_lines = [ln for ln in body.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert len(data_lines) == 1
    tokens = data_lines[0].split()
    assert MANUAL_LISTED_PLACEHOLDER in tokens


def test_export_cash_emits_current_value_only(app_client: TestClient) -> None:
    """Cash/wmf have no cost basis today — export is value-only, not
    shares/avg_cost, and (item 8) carries no trailing pricing_mode tag."""
    resp = app_client.post("/holdings", json=_PARSED_CASH)
    assert resp.status_code == 201
    body = app_client.get("/holdings/export").text
    data_lines = [ln for ln in body.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert len(data_lines) == 1
    line = data_lines[0]
    assert "15000" in line
    assert "USD Cash" in line
    assert "pricing_mode:" not in line


def test_create_holding_force_suffixes_psh_and_persists_market_uk(
    app_client: TestClient, db_session: Session
) -> None:
    resp = app_client.post("/holdings", json=_PARSED_PSH)
    assert resp.status_code == 201
    body = resp.json()
    assert body["ticker"] == "PSH.L"
    assert body["market"] == "UK"
    assert body["capture_supported"] is True
    holding = db_session.get(Holding, uuid.UUID(body["id"]))
    assert holding is not None
    assert holding.ticker == "PSH.L"
    assert holding.market == "UK"
    assert holding.capture_supported is True


def test_patch_holding_force_suffixes_bare_hk_ticker(
    app_client: TestClient, db_session: Session
) -> None:
    created = app_client.post("/holdings", json=_PARSED_APPLE).json()
    resp = app_client.patch(
        f"/holdings/{created['id']}",
        json={"ticker": "0700", "currency": "HKD", "market": "HK"},
    )
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "0700.HK"
    assert resp.json()["market"] == "HK"


def test_patch_holding_normalizes_hk_ticker_after_force_suffix(app_client: TestClient) -> None:
    """PR #310 round 5: the router's force-suffix path must canonicalize the
    same way file-import parsing does (_postprocess re-runs HK-normalize
    after applying a suffix) — a bare 3-digit HK code force-suffixed via the
    API must come out 4-digit zero-padded, matching the dialect-import path,
    or ticker_themes/config YAML lookups (keyed on the canonical form) miss
    for API-written holdings even though they hit for file-imported ones."""
    created = app_client.post("/holdings", json=_PARSED_APPLE).json()
    resp = app_client.patch(
        f"/holdings/{created['id']}",
        json={"ticker": "700", "currency": "HKD", "market": "HK"},
    )
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "0700.HK"


def test_create_holding_ambiguous_suffix_market_does_not_default_to_us_capture(
    app_client: TestClient,
) -> None:
    """PR #310 round 6 review: the router's _apply_write_defaults calls
    apply_confirmed_exchange_suffix + resolve_holding_market exactly like
    _postprocess does, so it shares the same bug — a bare EUR/KRW ticker (or
    an unplaceable A-share code) with no suffix must not default to
    market=US / capture_supported=True via market_from_ticker's bare-ticker
    fallback. Verifies the fix reaches the API write path, not just the
    file-import path."""
    row = {**_PARSED_APPLE, "ticker": "XYZ123", "currency": "EUR", "market": None}
    resp = app_client.post("/holdings", json=row)
    assert resp.status_code == 201
    body = resp.json()
    assert body["ticker"] == "XYZ123"
    assert body["market"] == "Europe"
    assert body["capture_supported"] is False


def test_patch_notes_only_still_clears_stale_price_when_write_defaults_rewrites_ticker(
    app_client: TestClient, db_session: Session
) -> None:
    """PR #310 round 5: `ticker_changed` must reflect what _apply_write_defaults
    actually wrote (it force-suffixes a ticker even on a PATCH that never
    mentions `ticker`), not just whether the client's PATCH body included a
    `ticker` key. A legacy unsuffixed row (stored before force-suffix
    existed) whose first-ever PATCH only touches `notes` must still get its
    stale sector/price cleared and a backfill enqueued — otherwise this is
    the round-1 regression (stale price survives a ticker change) reopened
    through a different door."""
    created = app_client.post("/holdings", json=_PARSED_APPLE).json()
    holding = db_session.get(Holding, uuid.UUID(created["id"]))
    assert holding is not None
    holding.ticker = "0700"
    holding.currency = "HKD"
    holding.market = "HK"
    holding.sector = "Technology"
    holding.market_price = Decimal("380")
    holding.price_as_of = datetime(2026, 1, 2, tzinfo=UTC)
    holding.price_fetched_at = holding.price_as_of
    db_session.commit()
    with patch("app.tasks.capture_tasks.backfill_sectors_task") as mock_sector:
        resp = app_client.patch(f"/holdings/{created['id']}", json={"notes": "annual review"})
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "0700.HK"
    db_session.expire_all()
    holding = db_session.get(Holding, uuid.UUID(created["id"]))
    assert holding is not None
    assert holding.ticker == "0700.HK"
    assert holding.sector is None
    assert holding.market_price is None
    assert holding.price_as_of is None
    assert holding.price_fetched_at is None
    mock_sector.delay.assert_called_once_with([created["id"]], str(TEST_USER_ID))


def test_reorder_locks_before_assigning_position(app_client: TestClient) -> None:
    a = app_client.post("/holdings", json=_PARSED_APPLE).json()
    b = app_client.post("/holdings", json=_PARSED_CASH).json()
    with patch("app.routers.holdings._lock_user_holdings") as mock_lock:
        resp = app_client.patch("/holdings/reorder", json={"ids": [b["id"], a["id"]]})
    assert resp.status_code == 200
    mock_lock.assert_called()
    assert [row["id"] for row in resp.json()] == [b["id"], a["id"]]
    assert [row["position"] for row in resp.json()] == [0, 1]
