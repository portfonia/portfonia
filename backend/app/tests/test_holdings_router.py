"""Integration tests for /holdings endpoints — real Postgres, mocked LLM."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

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


def test_upload_returns_preview(app_client: TestClient) -> None:
    content = (FIXTURES / "sample_holdings.md").read_bytes()
    with patch(
        "app.services.holding_parser.openai.OpenAI", return_value=_make_mock_client(_MOCK_PREVIEW)
    ):
        resp = app_client.post(
            "/holdings/upload",
            files={"file": ("holdings.md", content, "text/markdown")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["valid_rows"]) == 2
    assert len(body["issue_rows"]) == 1
    assert body["valid_rows"][0]["name"] == "Apple"


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
