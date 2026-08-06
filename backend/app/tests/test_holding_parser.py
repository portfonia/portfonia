"""Unit tests for holding_parser — LLM calls are mocked."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from app.core import llm
from app.core.config import get_settings
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


def test_extract_xlsx_preserves_leading_zero_codes(tmp_path: Path) -> None:
    """Leading-zero identifiers (00700 / 02333) must survive the xlsx read,
    not be coerced to ints before the LLM sees them. (#53)"""
    pytest.importorskip("pandas")
    import pandas as pd

    df = pd.DataFrame({"name": ["Tencent", "Great Wall"], "code": ["00700", "02333"]})
    xlsx_path = tmp_path / "h.xlsx"
    df.to_excel(xlsx_path, index=False)
    result = _extract_text(xlsx_path.read_bytes(), "h.xlsx")
    assert "00700" in result
    assert "02333" in result
    assert "700," not in result.replace("00700", "")


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
# _postprocess — HK ticker normalization (issue #49) + dedup (issue #50)
# ---------------------------------------------------------------------------


def _raw_row(**overrides: object) -> dict[str, object]:
    """A minimally-valid LLM row dict; override fields per test."""
    base: dict[str, object] = {
        "name": "Asset",
        "ticker": None,
        "fund_code": None,
        "currency": "USD",
        "shares": 1.0,
        "avg_cost": 1.0,
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
    base.update(overrides)
    return base


def test_normalize_hk_ticker_strips_leading_zero() -> None:
    rows = _postprocess([_raw_row(name="Great Wall", ticker="02333.HK", currency="HKD")])
    assert rows[0].ticker == "2333.HK"
    assert any("2333.HK" in issue for issue in rows[0].issues)


def test_normalize_hk_ticker_pads_short_code() -> None:
    rows = _postprocess([_raw_row(name="Tencent", ticker="700.HK", currency="HKD")])
    assert rows[0].ticker == "0700.HK"


def test_normalize_hk_ticker_leaves_canonical_unchanged() -> None:
    rows = _postprocess([_raw_row(name="Great Wall", ticker="2333.HK", currency="HKD")])
    assert rows[0].ticker == "2333.HK"
    assert rows[0].issues == []


def test_dedup_keeps_same_ticker_different_broker() -> None:
    """Two distinct VOO lots at different brokers must both survive. (issue #50)"""
    rows = _postprocess(
        [
            _raw_row(name="VOO", ticker="VOO", shares=316.0, avg_cost=618.0, broker="Schwab"),
            _raw_row(name="VOO", ticker="VOO", shares=10.0, avg_cost=600.0, broker="Schwba"),
        ]
    )
    assert len(rows) == 2


def test_dedup_collapses_identical_rows() -> None:
    """A truly byte-identical duplicate (LLM double-emit) still collapses."""
    rows = _postprocess(
        [
            _raw_row(name="VOO", ticker="VOO", shares=316.0, avg_cost=618.0, broker="Schwab"),
            _raw_row(name="VOO", ticker="VOO", shares=316.0, avg_cost=618.0, broker="Schwab"),
        ]
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# _summarize — per-broker cross-check (issue #51)
# ---------------------------------------------------------------------------


def test_summarize_groups_by_broker_in_upload_order() -> None:
    rows = _postprocess(
        [
            _raw_row(name="VOO", ticker="VOO", shares=2.0, avg_cost=600.0, broker="Schwab"),
            _raw_row(
                name="Tencent",
                ticker="2333.HK",
                shares=100.0,
                avg_cost=16.0,
                currency="HKD",
                broker="Futu",
            ),
            _raw_row(name="QQQM", ticker="QQQM", shares=1.0, avg_cost=200.0, broker="Schwab"),
        ]
    )
    from app.services.holding_parser import _summarize

    groups = _summarize(rows)
    assert [g.broker for g in groups] == ["Schwab", "Futu"]
    schwab = groups[0]
    assert schwab.holding_count == 2
    assert schwab.subtotals[0].currency == "USD"
    assert schwab.subtotals[0].cost_basis == 1400.0  # 2*600 + 1*200


def test_summarize_brokerless_rows_under_other() -> None:
    from app.services.holding_parser import _summarize

    rows = _postprocess(
        [
            _raw_row(
                name="Cash",
                ticker=None,
                shares=None,
                avg_cost=None,
                current_value=5000.0,
                pricing_mode="manual",
                asset_type="cash",
                broker=None,
            )
        ]
    )
    groups = _summarize(rows)
    assert groups[0].broker == "Other"
    assert groups[0].subtotals[0].cost_basis == 5000.0


def test_summarize_splits_mixed_currency_subtotals() -> None:
    from app.services.holding_parser import _summarize

    rows = _postprocess(
        [
            _raw_row(
                name="VOO", ticker="VOO", shares=1.0, avg_cost=600.0, currency="USD", broker="Futu"
            ),
            _raw_row(
                name="Great Wall",
                ticker="2333.HK",
                shares=100.0,
                avg_cost=16.0,
                currency="HKD",
                broker="Futu",
            ),
        ]
    )
    groups = _summarize(rows)
    currencies = {s.currency for s in groups[0].subtotals}
    assert currencies == {"USD", "HKD"}


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


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://example.test"))


def test_parse_uses_structured_model_pinned_to_openinference_bf16() -> None:
    mock_client = _make_mock_client(_MOCK_LLM_RESPONSE)
    with patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client):
        parse("some text")

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == get_settings().STRUCTURED_LLM_MODEL
    provider = kwargs["extra_body"]["provider"]
    assert provider["order"] == ["OpenInference"]
    assert provider["quantizations"] == ["bf16"]
    assert provider["allow_fallbacks"] is False
    # PR #79 review nit: the pin test covered precision but not the data-policy
    # half of structured_provider — holdings parsing is the highest-sensitivity
    # structured call, deny must hold on the pinned attempts too.
    assert provider["data_collection"] == get_settings().OPENROUTER_DATA_COLLECTION


def test_parse_retries_same_model_once_on_connection_error() -> None:
    mock_client = _make_mock_client(_MOCK_LLM_RESPONSE)
    mock_client.chat.completions.create.side_effect = [
        _connection_error(),
        mock_client.chat.completions.create.return_value,
    ]
    with patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client):
        result = parse("some holdings text")

    assert mock_client.chat.completions.create.call_count == 2
    calls = mock_client.chat.completions.create.call_args_list
    models_tried = [c.kwargs["model"] for c in calls]
    providers_tried = [c.kwargs["extra_body"]["provider"] for c in calls]
    assert models_tried[0] == models_tried[1]
    # Both attempts stay pinned to OpenInference/bf16 (issue #78) — the
    # open-provider degradation is reserved for the third attempt only.
    assert providers_tried[0] == providers_tried[1]
    assert providers_tried[0]["order"] == ["OpenInference"]
    assert result.valid_rows[0].name == "Apple"


def test_parse_degrades_to_open_provider_after_two_pinned_attempts_fail() -> None:
    """Issue #78: fallback is a provider-pin degradation, not a model swap —
    all three attempts use STRUCTURED_LLM_MODEL; only the third drops the
    OpenInference/bf16 pin and allows open provider selection."""
    mock_client = _make_mock_client(_MOCK_LLM_RESPONSE)
    success_response = mock_client.chat.completions.create.return_value
    mock_client.chat.completions.create.side_effect = [
        _connection_error(),
        _connection_error(),
        success_response,
    ]
    with patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client):
        result = parse("some holdings text")

    assert mock_client.chat.completions.create.call_count == 3
    calls = mock_client.chat.completions.create.call_args_list
    models_tried = [c.kwargs["model"] for c in calls]
    providers_tried = [c.kwargs["extra_body"]["provider"] for c in calls]
    assert models_tried[0] == models_tried[1] == models_tried[2]
    assert providers_tried[0] == providers_tried[1]
    assert providers_tried[0]["allow_fallbacks"] is False
    assert "order" not in providers_tried[2]
    assert "quantizations" not in providers_tried[2]
    assert providers_tried[2]["allow_fallbacks"] is True
    assert result.valid_rows[0].name == "Apple"


def test_parse_raises_after_retry_and_fallback_both_fail() -> None:
    mock_client = _make_mock_client(_MOCK_LLM_RESPONSE)
    mock_client.chat.completions.create.side_effect = [
        _connection_error(),
        _connection_error(),
        _connection_error(),
    ]
    with (
        patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client),
        pytest.raises(RuntimeError, match="LLM call failed"),
    ):
        parse("some holdings text")

    assert mock_client.chat.completions.create.call_count == 3


def test_structured_provider_pinned_locks_openinference_bf16_hard_fail() -> None:
    with patch("app.core.llm.get_settings", return_value=_fake_settings("", "deny")):
        provider = llm.structured_provider(pinned=True)
    assert provider == {
        "order": ["OpenInference"],
        "quantizations": ["bf16"],
        "allow_fallbacks": False,
        "data_collection": "deny",
    }


def test_structured_provider_open_allows_fallback_and_omits_pin() -> None:
    with patch("app.core.llm.get_settings", return_value=_fake_settings("", "deny")):
        provider = llm.structured_provider(pinned=False)
    assert provider == {"allow_fallbacks": True, "data_collection": "deny"}


def test_structured_provider_omits_data_collection_when_unset() -> None:
    with patch("app.core.llm.get_settings", return_value=_fake_settings("", "")):
        provider = llm.structured_provider(pinned=False)
    assert provider == {"allow_fallbacks": True}


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
