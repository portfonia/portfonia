"""Integration tests for /holdings endpoints — real Postgres, mocked LLM."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.models.upload_job import UploadJob
from app.tests.conftest import TEST_USER_ID

FIXTURES = Path(__file__).parent / "fixtures"

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
    job = UploadJob(
        user_id=uuid.uuid4(),  # not TEST_USER_ID
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
    resp = app_client.post("/holdings/confirm", json=[_PARSED_APPLE, _PARSED_CASH])
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    names = {r["name"] for r in rows}
    assert names == {"Apple", "USD Cash"}


def test_confirm_sets_last_manual_update_for_manual_rows(app_client: TestClient) -> None:
    resp = app_client.post("/holdings/confirm", json=[_PARSED_CASH])
    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["last_manual_update"] is not None


def test_confirm_full_replace_on_second_call(app_client: TestClient) -> None:
    app_client.post("/holdings/confirm", json=[_PARSED_APPLE, _PARSED_CASH])

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
    resp = app_client.post("/holdings/confirm", json=[tencent])
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
        resp = app_client.post("/holdings/confirm", json=[_PARSED_APPLE])
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
    db_session.add(
        Holding(
            user_id=uuid.uuid4(),
            name="Microsoft",
            ticker="MSFT",
            pricing_mode="auto",
            currency="USD",
            asset_class="STOCK",
        )
    )
    db_session.commit()

    with patch("app.tasks.capture_tasks.backfill_ohlcv_task") as mock_task:
        resp = app_client.post("/holdings/confirm", json=[_PARSED_APPLE])
    assert resp.status_code == 200
    mock_task.delay.assert_called_once_with(["AAPL"])


def test_confirm_skips_backfill_when_this_users_tickers_already_have_history(
    app_client: TestClient, db_session: Session
) -> None:
    """Someone else's sparse name must not fire a 420-day job on this confirm."""
    db_session.add(
        Holding(
            user_id=uuid.uuid4(),
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
        resp = app_client.post("/holdings/confirm", json=[_PARSED_APPLE])
    assert resp.status_code == 200
    mock_task.delay.assert_not_called()


def test_confirm_full_replace_does_not_touch_other_users(
    app_client: TestClient, db_session: Session
) -> None:
    """B-UAT-13 (Ring 1-B design doc §5.3/§10.2): confirm_holdings' full-
    replace DELETE is the one call in this codebase that wipes an entire
    user's holdings in one statement — it must stay scoped to the caller's
    own user_id no matter how identity resolution changes upstream."""
    other_user_id = uuid.uuid4()
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

    resp = app_client.post("/holdings/confirm", json=[_PARSED_APPLE, _PARSED_CASH])
    assert resp.status_code == 200

    remaining = db_session.query(Holding).filter(Holding.user_id == other_user_id).all()
    assert len(remaining) == 1
    assert remaining[0].name == "Other User's Fund"


def test_confirm_empty_list_clears_holdings(app_client: TestClient) -> None:
    app_client.post("/holdings/confirm", json=[_PARSED_APPLE])
    resp = app_client.post("/holdings/confirm", json=[])
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
    app_client.post("/holdings/confirm", json=[_PARSED_APPLE, _PARSED_CASH])
    resp = app_client.get("/holdings")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_list_holdings_includes_expected_fields(app_client: TestClient) -> None:
    app_client.post("/holdings/confirm", json=[_PARSED_APPLE])
    row = app_client.get("/holdings").json()[0]
    for field in ("id", "name", "ticker", "currency", "pricing_mode", "created_at", "updated_at"):
        assert field in row


# ---------------------------------------------------------------------------
# GET /holdings/export
# ---------------------------------------------------------------------------


def test_export_returns_markdown_file(app_client: TestClient) -> None:
    app_client.post("/holdings/confirm", json=[_PARSED_APPLE, _PARSED_CASH])
    resp = app_client.get("/holdings/export")
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    assert "holdings.md" in resp.headers["content-disposition"]
    body = resp.text
    assert "Apple" in body
    assert "AAPL" in body
    assert "USD Cash" in body


def test_export_empty_holdings(app_client: TestClient) -> None:
    resp = app_client.get("/holdings/export")
    assert resp.status_code == 200
    assert "# Holdings" in resp.text


def test_export_escapes_pipes_and_newlines_in_free_text(app_client: TestClient) -> None:
    """A pipe or newline in name/notes must not break the Markdown table."""
    row = {**_PARSED_APPLE, "name": "Acme | Corp", "notes": "line1\nline2"}
    app_client.post("/holdings/confirm", json=[row])
    body = app_client.get("/holdings/export").text

    # One holding → exactly 3 table lines (header + divider + 1 row). A raw
    # newline would have spilled the row; an unescaped pipe would have added a
    # column. Both must be neutralized.
    table_lines = [ln for ln in body.splitlines() if ln.startswith("|")]
    assert len(table_lines) == 3
    assert "Acme \\| Corp" in body
    assert "line1 line2" in body  # newline flattened, not split across rows
