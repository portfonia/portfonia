"""Unit tests for holding_parser — LLM calls are mocked."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core import llm
from app.schemas.holdings import UploadPreview
from app.services.holding_parser import (
    _extract_text,
    _postprocess,
    _strip_code_fence,
    parse,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------


def test_extract_md_strips_comments_and_returns_utf8() -> None:
    content = b"# Holdings\nAAPL USD 10 180\n##### comment\nTSLA USD 5 200"
    result = _extract_text(content, "holdings.md")
    assert "AAPL USD 10 180" in result
    assert "TSLA USD 5 200" in result
    assert "# Holdings" not in result
    assert "##### comment" not in result


def test_extract_txt() -> None:
    content = b"AAPL 10"
    assert _extract_text(content, "h.txt") == "AAPL 10"


def test_extract_csv() -> None:
    content = b"name,ticker\nApple,AAPL"
    assert _extract_text(content, "h.csv") == "name,ticker\nApple,AAPL"


def test_extract_csv_gbk_fallback() -> None:
    """A GBK-encoded CN export must decode via the gb18030 fallback, not 500."""
    content = "名称,代码\n招商银行,600036.SS".encode("gb18030")
    assert _extract_text(content, "h.csv") == "名称,代码\n招商银行,600036.SS"


def test_extract_unsupported_extension_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported file type"):
        _extract_text(b"data", "holdings.pdf")


def test_postprocess_normalizes_market_aliases() -> None:
    raw = [
        {"name": "A", "currency": "USD", "shares": 1, "pricing_mode": "auto", "market": "美股"},
        {"name": "B", "currency": "HKD", "shares": 1, "pricing_mode": "auto", "market": "港股"},
        {"name": "C", "currency": "CNY", "shares": 1, "pricing_mode": "auto", "market": "A股"},
        {"name": "D", "currency": "GBP", "shares": 1, "pricing_mode": "auto", "market": "UK"},
        {"name": "E", "currency": "USD", "shares": 1, "pricing_mode": "auto", "market": None},
    ]
    rows = _postprocess(raw)
    assert [r.market for r in rows] == ["US", "HK", "A-Share", "Other", None]


def test_postprocess_coerces_unknown_asset_type_to_null() -> None:
    raw = [
        {
            "name": "Mystery",
            "currency": "USD",
            "shares": 1,
            "pricing_mode": "auto",
            "asset_type": "crypto",
            "issues": [],
            "confidence": 0.9,
        }
    ]
    rows = _postprocess(raw)
    assert rows[0].asset_type is None
    assert any("crypto" in i for i in rows[0].issues)


def test_extract_xlsx_single_sheet(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    import pandas as pd

    df = pd.DataFrame({"name": ["Apple"], "ticker": ["AAPL"]})
    xlsx_path = tmp_path / "h.xlsx"
    df.to_excel(xlsx_path, index=False)
    result = _extract_text(xlsx_path.read_bytes(), "h.xlsx")
    assert "Apple" in result
    assert "AAPL" in result


def test_extract_xlsx_multi_sheet_raises(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    import pandas as pd

    xlsx_path = tmp_path / "h.xlsx"
    with pd.ExcelWriter(str(xlsx_path)) as writer:
        pd.DataFrame({"a": [1]}).to_excel(writer, sheet_name="Sheet1", index=False)
        pd.DataFrame({"b": [2]}).to_excel(writer, sheet_name="Sheet2", index=False)
    with pytest.raises(ValueError, match="2 sheets"):
        _extract_text(xlsx_path.read_bytes(), "h.xlsx")


def test_extract_sample_fixture() -> None:
    content = (FIXTURES / "sample_holdings.md").read_bytes()
    text = _extract_text(content, "sample_holdings.md")
    assert "AAPL" in text
    assert "0700.HK" in text


# ---------------------------------------------------------------------------
# _postprocess — currency correction
# ---------------------------------------------------------------------------


def test_postprocess_corrects_hk_ticker_currency() -> None:
    raw: list[dict[str, object]] = [
        {
            "name": "Tencent",
            "ticker": "0700.HK",
            "fund_code": None,
            "currency": "USD",  # wrong — should be corrected
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
            "confidence": 0.9,
        }
    ]
    rows = _postprocess(raw)
    assert rows[0].currency == "HKD"
    assert any("HKD" in issue for issue in rows[0].issues)


def test_postprocess_corrects_ss_ticker_currency() -> None:
    raw: list[dict[str, object]] = [
        {
            "name": "Moutai",
            "ticker": "600519.SS",
            "fund_code": None,
            "currency": "HKD",
            "shares": 10.0,
            "avg_cost": 1680.0,
            "current_value": None,
            "pricing_mode": "auto",
            "asset_type": "stock",
            "broker": None,
            "account": None,
            "portfolio": None,
            "notes": None,
            "issues": [],
            "confidence": 1.0,
        }
    ]
    rows = _postprocess(raw)
    assert rows[0].currency == "CNY"


def test_postprocess_no_correction_when_currency_correct() -> None:
    raw: list[dict[str, object]] = [
        {
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
    ]
    rows = _postprocess(raw)
    assert rows[0].currency == "USD"
    assert rows[0].issues == []


# ---------------------------------------------------------------------------
# parse — mocked LLM
# ---------------------------------------------------------------------------

_MOCK_LLM_RESPONSE = {
    "valid_rows": [
        {
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
        },
        {
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
        },
    ],
    "issue_rows": [
        {"raw": "??? totally gibberish", "reason": "Cannot identify asset name or value"}
    ],
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


def _make_mock_client_raw(content: str) -> MagicMock:
    mock_message = MagicMock()
    mock_message.content = content
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response
    return mock_client


def test_strip_code_fence_unwraps_json_fence() -> None:
    raw = '```json\n{"valid_rows": [], "issue_rows": []}\n```'
    assert _strip_code_fence(raw) == '{"valid_rows": [], "issue_rows": []}'


def test_strip_code_fence_unwraps_bare_fence() -> None:
    raw = '```\n{"a": 1}\n```'
    assert _strip_code_fence(raw) == '{"a": 1}'


def test_strip_code_fence_passthrough_when_no_fence() -> None:
    raw = '  {"a": 1}  '
    assert _strip_code_fence(raw) == '{"a": 1}'


def test_parse_handles_markdown_fenced_response() -> None:
    # Anthropic models on OpenRouter wrap JSON in a ```json fence despite
    # response_format=json_object. Parser must unwrap it.
    fenced = "```json\n" + json.dumps(_MOCK_LLM_RESPONSE) + "\n```"
    with patch(
        "app.services.holding_parser.openai.OpenAI",
        return_value=_make_mock_client_raw(fenced),
    ):
        result = parse("some holdings text")
    assert len(result.valid_rows) == 2
    assert result.valid_rows[0].name == "Apple"


def test_parse_returns_upload_preview() -> None:
    with patch(
        "app.services.holding_parser.openai.OpenAI",
        return_value=_make_mock_client(_MOCK_LLM_RESPONSE),
    ):
        result = parse("some holdings text")
    assert isinstance(result, UploadPreview)
    assert len(result.valid_rows) == 2
    assert len(result.issue_rows) == 1
    assert result.valid_rows[0].name == "Apple"
    assert result.issue_rows[0].raw == "??? totally gibberish"


def test_parse_valid_row_fields() -> None:
    with patch(
        "app.services.holding_parser.openai.OpenAI",
        return_value=_make_mock_client(_MOCK_LLM_RESPONSE),
    ):
        result = parse("some text")
    apple = result.valid_rows[0]
    assert apple.ticker == "AAPL"
    assert apple.currency == "USD"
    assert apple.pricing_mode == "auto"
    assert apple.shares == 10.0


def test_parse_issue_row_included() -> None:
    with patch(
        "app.services.holding_parser.openai.OpenAI",
        return_value=_make_mock_client(_MOCK_LLM_RESPONSE),
    ):
        result = parse("some text")
    assert result.issue_rows[0].reason == "Cannot identify asset name or value"


def test_parse_empty_response_returns_empty_preview() -> None:
    empty_payload: dict[str, object] = {"valid_rows": [], "issue_rows": []}
    with patch(
        "app.services.holding_parser.openai.OpenAI", return_value=_make_mock_client(empty_payload)
    ):
        result = parse("")
    assert result.valid_rows == []
    assert result.issue_rows == []


def test_parse_raises_on_invalid_json() -> None:
    mock_message = MagicMock()
    mock_message.content = "not json {"
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with (
        patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client),
        pytest.raises(RuntimeError, match="invalid JSON"),
    ):
        parse("text")


def test_parse_omits_provider_when_unset() -> None:
    mock_client = _make_mock_client(_MOCK_LLM_RESPONSE)
    with (
        patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client),
        patch("app.services.holding_parser.openrouter_provider", return_value=None),
    ):
        parse("some text")

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs.get("extra_body") is None


def test_parse_passes_provider_when_set() -> None:
    mock_client = _make_mock_client(_MOCK_LLM_RESPONSE)
    pinned = {"order": ["DigitalOcean", "Venice"], "allow_fallbacks": True}
    with (
        patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client),
        patch("app.services.holding_parser.openrouter_provider", return_value=pinned),
    ):
        parse("some text")

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"]["provider"] == pinned


def _fake_settings(order: str, data_collection: str, fallbacks: bool = True) -> MagicMock:
    fake = MagicMock()
    fake.OPENROUTER_PROVIDER_ORDER = order
    fake.OPENROUTER_DATA_COLLECTION = data_collection
    fake.OPENROUTER_ALLOW_FALLBACKS = fallbacks
    return fake


def test_openrouter_provider_none_when_all_empty() -> None:
    with patch("app.core.llm.get_settings", return_value=_fake_settings("", "")):
        assert llm.openrouter_provider() is None


def test_openrouter_provider_data_collection_only() -> None:
    with patch("app.core.llm.get_settings", return_value=_fake_settings("", "deny")):
        assert llm.openrouter_provider() == {
            "allow_fallbacks": True,
            "data_collection": "deny",
        }


def test_openrouter_provider_order_and_data_collection() -> None:
    with patch(
        "app.core.llm.get_settings",
        return_value=_fake_settings("DigitalOcean, Venice", "deny"),
    ):
        assert llm.openrouter_provider() == {
            "allow_fallbacks": True,
            "order": ["DigitalOcean", "Venice"],
            "data_collection": "deny",
        }
