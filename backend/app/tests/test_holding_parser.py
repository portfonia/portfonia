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
from app.schemas.holdings import ParsedRow, UploadPreview
from app.services import holding_parser as holding_parser_module
from app.services.holding_parser import (
    _classify_asset_class,
    _coerce_issue_list,
    _extract_text,
    _postprocess,
    _strip_code_fence,
    parse,
    parse_dialect_line,
    try_parse_dialect,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _issue_codes(row: ParsedRow) -> list[str]:
    return [i.code for i in row.issues]


def _issue_haystack(row: ParsedRow) -> str:
    parts: list[str] = []
    for issue in row.issues:
        parts.append(issue.code)
        parts.extend(str(v) for v in issue.params.values())
        parts.append(issue.severity)
    return " ".join(parts)


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


# ---------------------------------------------------------------------------
# Vocab-loaded module constants: _SYSTEM_PROMPT / _MARKET_ALIASES (#90 review)
# ---------------------------------------------------------------------------

_REQUIRED_PROMPT_TERMS = (
    "招商",
    "建设",
    "工商",
    "农业",
    "中信",
    "兴业",
    "浦发",
    "民生",
    "光大",
    "平安",
    "支付宝",
    "微信",
    "天天基金",
    "蚂蚁",
    "余额宝",
    "招财宝",
    "零钱通",
    "招行",
    "建行",
    "工行",
    "农行",
    "富途",
    "港股通",
    "现金",
    "保证金",
    "存款",
    "货币",
    "指数",
    "理财产品",
    "财富管理",
    "结构性存款",
    "代销",
    "A股",
    "沪深",
    "美股",
    "港股",
)


def test_system_prompt_contains_required_cn_examples() -> None:
    """Every Chinese example term from holding_parser_vocab.yml must survive
    into the built prompt. A deleted YAML entry would otherwise only show up
    as worse LLM extraction in production, with nothing catching it locally
    (PR #91 review)."""
    for term in _REQUIRED_PROMPT_TERMS:
        assert term in holding_parser_module._SYSTEM_PROMPT, f"missing from prompt: {term!r}"


def test_market_aliases_zh_keys_present() -> None:
    expected_zh_keys = {"美股", "美国", "港股", "香港", "a股", "沪深"}
    assert expected_zh_keys <= set(holding_parser_module._MARKET_ALIASES)


def test_postprocess_normalizes_market_aliases() -> None:
    raw = [
        {"name": "A", "currency": "USD", "shares": 1, "pricing_mode": "auto", "market": "美股"},
        {"name": "B", "currency": "HKD", "shares": 1, "pricing_mode": "auto", "market": "港股"},
        {"name": "C", "currency": "CNY", "shares": 1, "pricing_mode": "auto", "market": "A股"},
        # "UK" used to be an unsupported market literal that degraded to
        # Other via the alias fallback; #312 made UK a real capture market,
        # so it's now already-valid and passes through unchanged (test
        # updated post-#312 — PR #310 round 5 review).
        {"name": "D", "currency": "GBP", "shares": 1, "pricing_mode": "auto", "market": "UK"},
        {"name": "E", "currency": "USD", "shares": 1, "pricing_mode": "auto", "market": None},
    ]
    rows = _postprocess(raw)
    assert [r.market for r in rows] == ["US", "HK", "A-Share", "UK", None]


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
    assert "unrecognized_asset_type" in _issue_codes(rows[0])
    assert "crypto" in _issue_haystack(rows[0])


# ---------------------------------------------------------------------------
# _postprocess — cash/wmf ticker + shares coercion (issue #120)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "asset_type,identifier_field,identifier_value,extra",
    [
        ("cash", "ticker", "CASH", {"broker": "IBKR"}),
        ("wmf", "fund_code", "654321", {"currency": "CNY"}),
    ],
)
def test_postprocess_coerces_cash_wmf_row_with_fabricated_id_and_amount_in_shares(
    asset_type: str, identifier_field: str, identifier_value: str, extra: dict[str, object]
) -> None:
    """Regression for issue #120: production data showed the structured
    extraction model inventing a ticker "CASH" that wasn't in the source text
    at all, and putting the cash balance in `shares` instead of
    `current_value`. compute_portfolio()'s manual-pricing branch only reads
    `current_value`, so this silently dropped every cash/wmf holding out of
    every report. Parametrized cash+wmf (round-2 PR #121 nit: the wmf case
    previously only asserted fund_code/current_value/shares, not
    pricing_mode/issues like the cash case did)."""
    raw = [
        _raw_row(
            name="Bank WMP" if asset_type == "wmf" else "USD Cash",
            shares=199.98,
            avg_cost=None,
            current_value=None,
            pricing_mode="manual",
            asset_type=asset_type,
            **{identifier_field: identifier_value},
            **extra,
        )
    ]
    rows = _postprocess(raw)
    assert rows[0].ticker is None
    assert rows[0].fund_code is None
    assert rows[0].current_value == 199.98
    assert rows[0].shares is None
    assert rows[0].avg_cost is None
    assert rows[0].pricing_mode == "manual"
    assert "dropped_spurious_id" in _issue_codes(rows[0])
    assert identifier_value in _issue_haystack(rows[0])
    assert all(i.severity == "info" for i in rows[0].issues)


def test_postprocess_leaves_correctly_shaped_cash_row_unchanged() -> None:
    """A cash row already following the prompt's rules (no ticker, amount in
    current_value) must not be mangled or flagged."""
    raw = [
        _raw_row(
            name="现金",
            ticker=None,
            shares=None,
            avg_cost=None,
            current_value=100.0,
            pricing_mode="manual",
            asset_type="cash",
            currency="CNY",
        )
    ]
    rows = _postprocess(raw)
    assert rows[0].ticker is None
    assert rows[0].current_value == 100.0
    assert rows[0].market == "Other"
    assert rows[0].issues == []


@pytest.mark.parametrize("asset_type", ["cash", "wmf"])
def test_postprocess_forces_cash_wmf_row_from_auto_to_manual(asset_type: str) -> None:
    """Round-2 finding on PR #121: the earlier cash regression tests all set
    pricing_mode="manual" in the fixture already (matching the observed
    production shape), so they never proved the `row["pricing_mode"] =
    "manual"` line actually flips a model that emitted "auto" — which the
    prompt's own inference rule invites whenever a (fabricated) ticker is
    present ("A ticker or fund_code is present -> auto"). That force is
    load-bearing: without it, a cash/wmf row with a fabricated ticker would
    still get moved to current_value but stay pricing_mode="auto", and
    compute_portfolio()'s auto branch has no ticker to fetch a price for
    either — same silent drop, different branch. Parametrized cash+wmf
    (round-2 nit: previously cash-only)."""
    raw = [
        _raw_row(
            name="USD Cash",
            ticker="CASH",
            shares=199.98,
            avg_cost=None,
            current_value=None,
            pricing_mode="auto",
            asset_type=asset_type,
        )
    ]
    rows = _postprocess(raw)
    assert rows[0].pricing_mode == "manual"
    assert rows[0].current_value == 199.98


def test_postprocess_prefers_current_value_and_clears_residual_shares_when_both_set() -> None:
    """Round-2 finding on PR #121: leaving a dual-populated row's stray
    `shares`/`avg_cost` in place (the round-1 fix's behavior) isn't fully
    inert — `_row_cost_basis()` prefers `shares*avg_cost` over
    `current_value` whenever both `shares` AND `avg_cost` are non-null, so
    a residual pair would surface a wrong number in the upload-preview
    broker cost-basis subtotal (`_summarize()`) even though
    `compute_portfolio()`'s report valuation stays correct (it never reads
    shares). Fix: once current_value is settled as the source of truth for
    a cash/wmf row, shares/avg_cost are always cleared — current_value is
    kept as originally given, never overwritten by shares' value, since
    there's no reliable way to tell which of two conflicting numbers is
    real."""
    raw = [
        _raw_row(
            name="USD Cash",
            ticker=None,
            shares=199.98,
            avg_cost=1.0,
            current_value=50.0,
            pricing_mode="manual",
            asset_type="cash",
        )
    ]
    rows = _postprocess(raw)
    assert rows[0].current_value == 50.0
    assert rows[0].shares is None
    assert rows[0].avg_cost is None


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
    assert "currency_corrected" in _issue_codes(rows[0])
    assert "HKD" in _issue_haystack(rows[0])


def test_postprocess_ticker_correction_of_unrecognized_currency_leaves_no_stale_issue() -> None:
    """Regression (PR #114 review round 2): the LLM emitting a currency
    that isn't in VALID_CURRENCIES at all (not just wrong) must not leave a
    stale "Unrecognized currency" note once the ticker suffix corrects it
    to a valid one — the unrecognized-currency check must run AFTER
    ticker-suffix correction, not before."""
    raw = [_raw_row(name="Tencent", ticker="0700.HK", currency="RMB")]
    rows = _postprocess(raw)
    assert rows[0].currency == "HKD"
    assert "unrecognized_currency" not in _issue_codes(rows[0])
    assert "currency_corrected" in _issue_codes(rows[0])


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
# _postprocess — currency validation degrades per-row, doesn't fail the batch
# (issue #25/PR #114 review)
# ---------------------------------------------------------------------------


def test_postprocess_normalizes_currency_case() -> None:
    raw: list[dict[str, object]] = [
        {
            "name": "Apple",
            "ticker": None,
            "fund_code": None,
            "currency": "usd",  # lowercase — must not trip the exact-match check
            "shares": 10.0,
            "avg_cost": 100.0,
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
    assert rows[0].currency == "USD"
    assert "currency_normalized" in _issue_codes(rows[0])


def test_postprocess_drops_row_with_unrecognized_currency_without_raising() -> None:
    """A single bad currency must not kill the whole batch (previously a bare
    ValidationError from ParsedRow.model_validate propagated out of
    _postprocess and failed every other row in the same upload)."""
    raw = [
        _raw_row(name="Apple", currency="USD"),
        _raw_row(name="Bogus", currency="ZZZ"),
    ]
    rows = _postprocess(raw)
    assert [r.name for r in rows] == ["Apple"]


def test_postprocess_invokes_on_invalid_row_callback() -> None:
    rejected: list[tuple[dict[str, object], str]] = []
    raw = [_raw_row(name="Bogus", currency="ZZZ")]
    rows = _postprocess(raw, on_invalid_row=lambda row, reason: rejected.append((row, reason)))
    assert rows == []
    assert len(rejected) == 1
    assert "ZZZ" in rejected[0][1]


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
    assert "ticker_normalized_hk" in _issue_codes(rows[0])
    assert "2333.HK" in _issue_haystack(rows[0])


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


def test_parse_client_uses_bounded_timeout_and_no_sdk_retries() -> None:
    """Issue #77: each attempt must be time-bounded and the SDK's own
    retry-with-backoff disabled — parse() already implements its own
    2-attempt retry loop (issue #84), so a redundant SDK-level retry
    (default: max_retries=2, 600s read timeout) would only stack
    unpredictable extra latency on top, which is how a single request was
    observed taking ~5min."""
    mock_openai_cls = MagicMock(return_value=_make_mock_client(_MOCK_LLM_RESPONSE))
    with patch("app.services.holding_parser.openai.OpenAI", mock_openai_cls):
        parse("some holdings text")

    kwargs = mock_openai_cls.call_args.kwargs
    assert kwargs["timeout"] == holding_parser_module._PARSE_ATTEMPT_TIMEOUT_SECONDS
    assert kwargs["max_retries"] == 0


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


def test_parse_moves_invalid_currency_row_to_issue_rows() -> None:
    """End-to-end: a row with an unrecognized currency degrades to an
    issue_row instead of failing the whole parse (issue #25/PR #114
    review) — the other valid row in the same response still succeeds."""
    payload: dict[str, object] = {
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
                "broker": None,
                "account": None,
                "portfolio": None,
                "notes": None,
                "issues": [],
                "confidence": 1.0,
            },
            {
                "name": "Bogus",
                "ticker": None,
                "fund_code": None,
                "currency": "ZZZ",
                "shares": 1.0,
                "avg_cost": None,
                "current_value": None,
                "pricing_mode": "auto",
                "asset_type": None,
                "broker": None,
                "account": None,
                "portfolio": None,
                "notes": None,
                "issues": [],
                "confidence": 1.0,
            },
        ],
        "issue_rows": [],
    }
    with patch(
        "app.services.holding_parser.openai.OpenAI",
        return_value=_make_mock_client(payload),
    ):
        result = parse("some text")
    assert [r.name for r in result.valid_rows] == ["Apple"]
    assert len(result.issue_rows) == 1
    assert "ZZZ" in result.issue_rows[0].reason


def test_parse_empty_response_returns_empty_preview() -> None:
    empty_payload: dict[str, object] = {"valid_rows": [], "issue_rows": []}
    with patch(
        "app.services.holding_parser.openai.OpenAI", return_value=_make_mock_client(empty_payload)
    ):
        result = parse("")
    assert result.valid_rows == []
    assert result.issue_rows == []


def test_parse_raises_on_invalid_json_after_retrying() -> None:
    """A malformed body is retryable (#55): json.loads used to sit OUTSIDE the
    attempt loop, so one bad body failed the upload with the second attempt
    still unspent — the single most retry-worthy failure mode was the only one
    never retried."""
    mock_client = _make_mock_client_raw("not json {")

    with (
        patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client),
        pytest.raises(RuntimeError, match="invalid_json"),
    ):
        parse("text")

    assert mock_client.chat.completions.create.call_count == 2


@pytest.mark.parametrize("bad_body", ["null", "[]", '"a string"', "42"])
def test_parse_retries_json_of_the_wrong_shape(bad_body: str) -> None:
    """`json.loads` accepts null/list/string/number just fine — none of them
    honour the requested JSON-object shape. Before the fix, `null` slipped
    past the loop as a false "success" (payload=None misread later as "every
    attempt failed"), and list/string/number died on `.get()` as an
    AttributeError routed through holdings_tasks' unexpected-Exception path
    instead of its RuntimeError parse-failure path (PR #161 review)."""
    bad = _make_mock_client_raw(bad_body).chat.completions.create.return_value
    good = _make_mock_client(_MOCK_LLM_RESPONSE).chat.completions.create.return_value
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [bad, good]

    with patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client):
        result = parse("some holdings text")

    assert mock_client.chat.completions.create.call_count == 2
    assert result.valid_rows[0].name == "Apple"


def test_parse_raises_when_every_attempt_returns_wrong_shape_json() -> None:
    mock_client = _make_mock_client_raw("null")

    with (
        patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client),
        pytest.raises(RuntimeError, match="invalid_json"),
    ):
        parse("text")

    assert mock_client.chat.completions.create.call_count == 2


def test_parse_recovers_when_second_attempt_returns_valid_json() -> None:
    """The point of retrying a malformed body: the upload succeeds instead of
    failing the user for one non-deterministic miss."""
    good = _make_mock_client(_MOCK_LLM_RESPONSE).chat.completions.create.return_value
    bad = _make_mock_client_raw("not json {").chat.completions.create.return_value
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [bad, good]

    with patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client):
        result = parse("some holdings text")

    assert mock_client.chat.completions.create.call_count == 2
    assert result.valid_rows[0].name == "Apple"


def test_parse_retries_malformed_200_with_no_choices() -> None:
    """OpenRouter has been observed returning a 200 with choices=None (the
    same fault report_llm has guarded since I-DEBT-2). Indexing it raised a
    TypeError, which is not an openai.OpenAIError and so escaped the retry
    loop entirely — a retryable provider fault that failed the upload on the
    first occurrence (#55)."""
    empty = MagicMock()
    empty.choices = None
    good = _make_mock_client(_MOCK_LLM_RESPONSE).chat.completions.create.return_value
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [empty, good]

    with patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client):
        result = parse("some holdings text")

    assert mock_client.chat.completions.create.call_count == 2
    assert result.valid_rows[0].name == "Apple"


def test_parse_treats_empty_body_as_retryable_not_empty_portfolio() -> None:
    """An empty message body used to coerce to "{}" — surfacing as a
    *successful* upload with zero rows parsed rather than a retryable
    failure, which reads to the user as "my file was understood and it's
    empty" (#55)."""
    blank = _make_mock_client_raw("   ").chat.completions.create.return_value
    good = _make_mock_client(_MOCK_LLM_RESPONSE).chat.completions.create.return_value
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [blank, good]

    with patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client):
        result = parse("some holdings text")

    assert mock_client.chat.completions.create.call_count == 2
    assert result.valid_rows[0].name == "Apple"


def test_parse_retries_408_request_timeout() -> None:
    """Same SDK gap as report_llm's: OpenRouter's 408 arrives as a bare
    APIStatusError, not APITimeoutError (PR #161 review)."""
    err = openai.APIStatusError(
        "408",
        response=httpx.Response(408, request=httpx.Request("POST", "https://x.test")),
        body=None,
    )
    good = _make_mock_client(_MOCK_LLM_RESPONSE).chat.completions.create.return_value
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [err, good]

    with patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client):
        result = parse("some holdings text")

    assert mock_client.chat.completions.create.call_count == 2
    assert result.valid_rows[0].name == "Apple"


def test_parse_does_not_retry_a_non_retryable_auth_failure() -> None:
    """A bad API key reproduces identically on attempt 2. The pre-#55 blanket
    `except openai.OpenAIError` retried it anyway, spending up to 20s of a 45s
    SLA to reach the same failure."""
    err = openai.AuthenticationError(
        "invalid api key",
        response=httpx.Response(401, request=httpx.Request("POST", "https://openrouter.test/x")),
        body=None,
    )
    mock_client = _make_mock_client(_MOCK_LLM_RESPONSE)
    mock_client.chat.completions.create.side_effect = err

    with (
        patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client),
        pytest.raises(RuntimeError, match="auth"),
    ):
        parse("text")

    assert mock_client.chat.completions.create.call_count == 1


def test_parse_does_not_retry_a_malformed_request() -> None:
    """Same reasoning as the auth case, for a 400 we constructed ourselves."""
    err = openai.BadRequestError(
        "bad request",
        response=httpx.Response(400, request=httpx.Request("POST", "https://openrouter.test/x")),
        body=None,
    )
    mock_client = _make_mock_client(_MOCK_LLM_RESPONSE)
    mock_client.chat.completions.create.side_effect = err

    with (
        patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client),
        pytest.raises(RuntimeError, match="bad_request"),
    ):
        parse("text")

    assert mock_client.chat.completions.create.call_count == 1


def test_parse_does_not_launder_programming_errors_into_runtimeerror() -> None:
    """A blanket `except Exception` would classify a genuine bug as UNKNOWN
    and re-raise it as RuntimeError, routing it through holdings_tasks'
    normal parse-failure path (`logger.warning`) instead of its unexpected-
    exception path (`logger.exception`, full traceback) — the outer handler
    this loop must not preempt (PR #161 review, round 2)."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = TypeError("boom - programming bug")

    with (
        patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client),
        pytest.raises(TypeError, match="boom - programming bug"),
    ):
        parse("text")

    assert mock_client.chat.completions.create.call_count == 1


def test_parse_does_not_launder_soft_time_limit_exceeded() -> None:
    """Same reasoning as the programming-error case, for the concrete signal
    that motivated it: holdings_tasks.py runs this under a soft_time_limit,
    and its own comment says SoftTimeLimitExceeded is meant to be caught by
    the task's outer except/finally, not swallowed inside parse()."""
    from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = SoftTimeLimitExceeded()

    with (
        patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client),
        pytest.raises(SoftTimeLimitExceeded),
    ):
        parse("text")

    assert mock_client.chat.completions.create.call_count == 1


def test_parse_never_sleeps_between_attempts() -> None:
    """parse() runs under holdings_tasks' 45s SLA with each attempt capped at
    20s. Sharing the taxonomy with _call_llm must NOT drag in its backoff
    waits (llm_retry.yml's connection sequence alone is 30s+90s) — that would
    blow the hard time limit and get the worker SIGKILLed."""
    mock_client = _make_mock_client(_MOCK_LLM_RESPONSE)
    mock_client.chat.completions.create.side_effect = [
        _connection_error(),
        mock_client.chat.completions.create.return_value,
    ]

    with (
        patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client),
        patch("app.services.holding_parser.time.sleep") as sleep,
    ):
        parse("some holdings text")

    sleep.assert_not_called()


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://example.test"))


def test_parse_uses_structured_model_with_deny_and_no_reasoning() -> None:
    mock_client = _make_mock_client(_MOCK_LLM_RESPONSE)
    with patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client):
        parse("some text")

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == get_settings().STRUCTURED_LLM_MODEL
    provider = kwargs["extra_body"]["provider"]
    assert provider["allow_fallbacks"] is True
    # Issue #84: STRUCTURED_LLM_MODEL (openai/gpt-5.6-luna) has no
    # third-party-quantization pin concern the way the previous open-weight
    # model did — no "order"/"quantizations" pin — but deny must still hold,
    # holdings parsing is the highest-sensitivity structured call.
    assert "order" not in provider
    assert "quantizations" not in provider
    assert provider["data_collection"] == get_settings().OPENROUTER_DATA_COLLECTION
    # gpt-5.6-luna defaults reasoning to "medium" — wasted cost/latency for
    # mechanical extraction (issue #84).
    assert kwargs["extra_body"]["reasoning"] == {"effort": "none"}


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
    assert providers_tried[0] == providers_tried[1]
    assert result.valid_rows[0].name == "Apple"


def test_parse_raises_after_both_attempts_fail() -> None:
    mock_client = _make_mock_client(_MOCK_LLM_RESPONSE)
    mock_client.chat.completions.create.side_effect = [
        _connection_error(),
        _connection_error(),
    ]
    with (
        patch("app.services.holding_parser.openai.OpenAI", return_value=mock_client),
        pytest.raises(RuntimeError, match="LLM call failed"),
    ):
        parse("some holdings text")

    assert mock_client.chat.completions.create.call_count == 2


def test_structured_provider_allows_fallback_with_deny() -> None:
    with patch("app.core.llm.get_settings", return_value=_fake_settings("", "deny")):
        provider = llm.structured_provider()
    assert provider == {"allow_fallbacks": True, "data_collection": "deny"}


def test_structured_provider_omits_data_collection_when_unset() -> None:
    with patch("app.core.llm.get_settings", return_value=_fake_settings("", "")):
        provider = llm.structured_provider()
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


# ---------------------------------------------------------------------------
# _classify_asset_class — sibling fund_code consistency (issue #296)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fund_code", "expected"),
    [
        ("513100", "EQUITY_US_TECH"),
        ("513300", "EQUITY_US_TECH"),
        ("513500", "EQUITY_US_BROAD"),
        ("513650", "EQUITY_US_BROAD"),
        ("518660", "PRECIOUS_METALS"),
        ("518800", "PRECIOUS_METALS"),
        ("518850", "PRECIOUS_METALS"),
        ("518880", "PRECIOUS_METALS"),
    ],
)
def test_asset_class_fund_codes_share_sibling_bucket(fund_code: str, expected: str) -> None:
    """Every A-share ETF tracking the same index/commodity must land in the same
    economic-exposure bucket as its sibling products (issue #296 — 513500/518850
    etc. fell through to the generic fund catch-all)."""
    assert (
        holding_parser_module._classify_asset_class(
            {"ticker": None, "fund_code": fund_code, "asset_type": "fund"}
        )
        == expected
    )


# ---------------------------------------------------------------------------
# _classify_asset_class — hot-reloadable YAML mapping (issue #296)
# ---------------------------------------------------------------------------


def test_ticker_asset_class_mapping_reloads_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mapping must be editable without a code deploy — an admin adding a
    new fund_code to the YAML takes effect on the next parse with no process
    restart (issue #296 structural decision, the #35 live-reload property).
    The loader re-reads the file on every call, so overwriting the same path
    (no cache to clear) is what the test exercises."""
    mapping = tmp_path / "ticker_asset_class.yml"
    mapping.write_text(
        "ticker_asset_class:\n  QQQ: EQUITY_US_TECH\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(holding_parser_module, "_get_ticker_asset_class_path", lambda: mapping)

    # Version 1: 999999 is unmapped → generic fund catch-all.
    assert _classify_asset_class({"ticker": "999999", "asset_type": "etf"}) == "EQUITY_BROAD"

    # Admin adds the fund_code in place; the same process picks it up with
    # no cache clear and no restart.
    mapping.write_text(
        "ticker_asset_class:\n  QQQ: EQUITY_US_TECH\n  999999: EQUITY_DM\n",
        encoding="utf-8",
    )
    assert _classify_asset_class({"ticker": "999999", "asset_type": "etf"}) == "EQUITY_DM"


def test_ticker_asset_class_rejects_unknown_taxonomy_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd asset_class VALUE must fail loudly at load time, not silently
    classify every matching instrument into an unknown bucket and only die
    later in ParsedRow validation (issue #296 — same closed-taxonomy
    discipline as asset_class_thresholds.yml)."""
    mapping = tmp_path / "ticker_asset_class.yml"
    mapping.write_text(
        "ticker_asset_class:\n  QQQ: EQUITY_US_TEHC\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(holding_parser_module, "_get_ticker_asset_class_path", lambda: mapping)
    with pytest.raises(ValueError, match="EQUITY_US_TEHC"):
        _classify_asset_class({"ticker": "QQQ", "asset_type": "etf"})


def test_ticker_asset_class_rejects_missing_top_level_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config missing the top-level `ticker_asset_class` map must raise
    rather than silently classifying every instrument via the catch-all."""
    mapping = tmp_path / "ticker_asset_class.yml"
    mapping.write_text("other_section: {}\n", encoding="utf-8")
    monkeypatch.setattr(holding_parser_module, "_get_ticker_asset_class_path", lambda: mapping)
    with pytest.raises(ValueError, match="ticker_asset_class"):
        _classify_asset_class({"ticker": "QQQ", "asset_type": "etf"})


def test_postprocess_overrides_cash_market_a_share_from_mainland_broker() -> None:
    """CMB USD cash must be Other (listing venue), not A-Share inferred from the bank."""
    raw = [
        _raw_row(
            name="USD Cash",
            ticker=None,
            shares=None,
            avg_cost=None,
            current_value=15000.0,
            pricing_mode="manual",
            asset_type="cash",
            currency="USD",
            broker="CMB",
            market="A-Share",
            confidence=1.0,
        )
    ]
    rows = _postprocess(raw)
    assert rows[0].market == "Other"
    assert rows[0].confidence == 1.0
    assert not any(i.severity == "warning" for i in rows[0].issues)


def test_postprocess_high_confidence_cash_is_not_warning() -> None:
    raw = [
        _raw_row(
            name="USD Cash",
            ticker="CASH",
            shares=199.98,
            current_value=None,
            pricing_mode="manual",
            asset_type="cash",
            currency="USD",
            broker="CMB",
            confidence=0.95,
        )
    ]
    rows = _postprocess(raw)
    assert rows[0].market == "Other"
    assert rows[0].ticker is None
    assert all(i.severity == "info" for i in rows[0].issues)
    assert "dropped_spurious_id" in _issue_codes(rows[0])
    assert "cash_amount_moved" in _issue_codes(rows[0])


def test_postprocess_does_not_reclassify_listed_ticker_into_other() -> None:
    raw = [_raw_row(name="Pershing Square", ticker="PSH", currency="GBP")]
    rows = _postprocess(raw)
    assert rows[0].ticker == "PSH.L"
    assert rows[0].market == "UK"
    assert rows[0].capture_supported is True


def test_postprocess_force_suffixes_psh_when_market_uk() -> None:
    """Confirmed UK + GBP applies .L; UK is a real capture market after #312."""
    raw = [_raw_row(name="Pershing Square", ticker="PSH", currency="GBP", market="UK")]
    rows = _postprocess(raw)
    assert rows[0].ticker == "PSH.L"
    assert rows[0].market == "UK"
    assert rows[0].capture_supported is True
    assert "ticker_no_suffix" not in _issue_codes(rows[0])


def test_postprocess_force_suffixes_psh_via_override_without_market_tag() -> None:
    raw = [_raw_row(name="Pershing Square", ticker="PSH", currency="GBP")]
    rows = _postprocess(raw)
    assert rows[0].ticker == "PSH.L"
    assert rows[0].market == "UK"
    assert rows[0].capture_supported is True
    assert "ticker_no_suffix" not in _issue_codes(rows[0])


def test_postprocess_bare_gbp_ticker_force_suffixes_uk() -> None:
    """GBP now confirms UK, so .L is stored and capture is supported."""
    raw = [_raw_row(name="Unknown London name", ticker="VOD", currency="GBP")]
    rows = _postprocess(raw)
    assert rows[0].ticker == "VOD.L"
    assert rows[0].market == "UK"
    assert rows[0].capture_supported is True
    assert "ticker_no_suffix" not in _issue_codes(rows[0])


def test_postprocess_does_not_suffix_unresolvable_other() -> None:
    """Unresolvable suffix stays Other + not-processed; no speculative .L."""
    raw = [_raw_row(name="BHP Group", ticker="BHP.AX", currency="AUD", market="Other")]
    rows = _postprocess(raw)
    assert rows[0].ticker == "BHP.AX"
    assert rows[0].market == "Other"
    assert rows[0].capture_supported is False
    assert "ticker_no_suffix" not in _issue_codes(rows[0])


def test_postprocess_force_suffixes_hk_and_a_share_when_market_confirmed() -> None:
    hk = _postprocess([_raw_row(name="Tencent", ticker="0700", currency="HKD", market="HK")])
    assert hk[0].ticker == "0700.HK"
    assert hk[0].market == "HK"
    ss = _postprocess([_raw_row(name="Moutai", ticker="600519", currency="CNY", market="A-Share")])
    assert ss[0].ticker == "600519.SS"
    assert ss[0].market == "A-Share"
    sz = _postprocess(
        [_raw_row(name="Ping An Bank", ticker="000001", currency="CNY", market="A-Share")]
    )
    assert sz[0].ticker == "000001.SZ"
    assert sz[0].market == "A-Share"


def test_postprocess_suffixed_gbp_ticker_does_not_warn() -> None:
    raw = [_raw_row(name="Pershing Square", ticker="PSH.L", currency="GBP")]
    rows = _postprocess(raw)
    assert rows[0].ticker == "PSH.L"
    assert rows[0].market == "UK"
    assert rows[0].capture_supported is True
    assert "ticker_no_suffix" not in _issue_codes(rows[0])


def test_postprocess_unsupported_count_matches_preview_contract() -> None:
    """Issue #311: unresolvable rows stay valid; count is the heads-up summary."""
    rows = _postprocess(
        [
            {
                "name": "Vodafone",
                "ticker": "VOD.L",
                "currency": "GBP",
                "shares": 10,
                "pricing_mode": "auto",
            },
            {
                "name": "BHP Group",
                "ticker": "BHP.AX",
                "currency": "AUD",
                "shares": 10,
                "pricing_mode": "auto",
            },
            {
                "name": "Shopify",
                "ticker": "SHOP.TO",
                "currency": "CAD",
                "shares": 5,
                "pricing_mode": "auto",
            },
        ]
    )
    assert [r.capture_supported for r in rows] == [True, False, False]
    assert sum(1 for r in rows if not r.capture_supported) == 2


def test_postprocess_corrects_new_market_suffix_currencies() -> None:
    """Issue #311 / PR #312 B3: EUR/JPY/KRW suffix map, .L stays GBP."""
    cases = [
        ("ASML.AS", "EUR"),
        ("MC.PA", "EUR"),
        ("SAP.DE", "EUR"),
        ("7203.T", "JPY"),
        ("005930.KS", "KRW"),
        ("035420.KQ", "KRW"),
        ("VOD.L", "GBP"),
    ]
    for ticker, expected in cases:
        rows = _postprocess([_raw_row(name=ticker, ticker=ticker, currency="USD")])
        assert rows[0].currency == expected, ticker


def test_ticker_no_suffix_warns_for_europe_and_korea_currencies() -> None:
    """PR #310 round 5: the hint tuple predates #312's UK/Europe/Japan/Korea
    widening. EUR and KRW resolve to a real (post-#312) capture market but an
    ambiguous suffix (Europe/Korea each have multiple listing suffixes, so no
    suffix is guessed) — the hint must still fire for them, not just the
    original GBP/HKD/CNY set."""
    for currency in ("EUR", "KRW"):
        rows = _postprocess([_raw_row(name="Unknown listing", ticker="XYZ123", currency=currency)])
        assert "ticker_no_suffix" in _issue_codes(rows[0]), currency


def test_apply_confirmed_exchange_suffix_skips_manual_pricing_mode() -> None:
    """A manual-priced 'ticker' (e.g. real estate) is a free-text label, not a
    market symbol. Forcing a suffix serves no purpose — manual rows are never
    auto-captured (`_market_tickers` only selects pricing_mode == 'auto') —
    and corrupts the label. Mirrors the existing cash/wmf early return."""
    raw = [
        _raw_row(
            name="Family house",
            ticker="HOME",
            currency="HKD",
            pricing_mode="manual",
            asset_type="other",
        )
    ]
    rows = _postprocess(raw)
    assert rows[0].ticker == "HOME"
    assert "ticker_no_suffix" not in _issue_codes(rows[0])


# ---------------------------------------------------------------------------
# PR #310: drop LLM free-text notes; dialect round-trip
# ---------------------------------------------------------------------------


def test_coerce_issue_list_drops_llm_free_text_and_unknown_codes() -> None:
    raw = [
        "Inferred currency from broker context",
        {"code": "made_up_llm_code", "params": {"message": "hello"}, "severity": "warning"},
        {
            "code": "ticker_no_suffix",
            "params": {"ticker": "PSH", "currency": "GBP"},
            "severity": "warning",
        },
        {"code": "parser_note", "params": {"message": "English leftover"}, "severity": "info"},
    ]
    out = _coerce_issue_list(raw)
    assert [i["code"] for i in out] == ["ticker_no_suffix"]


def test_postprocess_drops_model_supplied_parser_notes() -> None:
    raw = [
        {
            "name": "Apple",
            "ticker": "AAPL",
            "currency": "USD",
            "shares": 10,
            "avg_cost": 180,
            "pricing_mode": "auto",
            "asset_type": "stock",
            "issues": ["guessed this was a US listing"],
            "confidence": 0.6,
        }
    ]
    rows = _postprocess(raw)
    assert rows[0].issues == []
    assert rows[0].confidence == 0.6


def test_parse_dialect_line_listed_and_cash_with_tags() -> None:
    listed = parse_dialect_line(
        'Apple AAPL USD 100 228 IBKR account:IRA portfolio:"Growth Sleeve" '
        'notes:"core holding" asset_type:stock market:US pricing_mode:auto'
    )
    assert listed is not None
    assert listed["name"] == "Apple"
    assert listed["ticker"] == "AAPL"
    assert listed["account"] == "IRA"
    assert listed["portfolio"] == "Growth Sleeve"
    assert listed["notes"] == "core holding"
    assert listed["asset_type"] == "stock"
    assert listed["market"] == "US"
    cash = parse_dialect_line(
        "USD Cash 50000 USD Schwab asset_type:cash market:Other pricing_mode:manual"
    )
    assert cash is not None
    assert cash["name"] == "USD Cash"
    assert cash["current_value"] == 50000.0
    assert cash["asset_type"] == "cash"
    wmf = parse_dialect_line(
        "Bank product 100000 CNY CMB asset_type:wealth-management market:Other pricing_mode:manual"
    )
    assert wmf is not None
    assert wmf["asset_type"] == "wmf"


def test_try_parse_dialect_requires_tags_else_none() -> None:
    untagged = "Apple AAPL USD 100 228 IBKR"
    assert try_parse_dialect(untagged) is None
    tagged = "Apple AAPL USD 100 228 IBKR asset_type:stock market:US pricing_mode:auto"
    rows = try_parse_dialect(tagged)
    assert rows is not None
    assert rows[0]["ticker"] == "AAPL"


def test_try_parse_dialect_mixed_tags_does_not_divert_whole_file() -> None:
    """One tagged line must not pull an untagged sibling onto positional parse."""
    mixed = "Apple AAPL USD 100 228 IBKR notes:reviewed\nMicrosoft MSFT USD 20 400 IBKR\n"
    assert try_parse_dialect(mixed) is None


def test_parse_dialect_invalid_row_surfaces_as_issue_row_without_llm() -> None:
    """Tokenized-but-invalid dialect rows stay on the fast path as issue_rows."""
    good = "Apple AAPL USD 10 180 IBKR asset_type:stock market:US pricing_mode:auto"
    bad_shares = "Apple AAPL USD -5 228 IBKR asset_type:stock market:US pricing_mode:auto"
    bad_cash = "USD Cash -100 USD Schwab asset_type:cash market:Other pricing_mode:manual"
    text = f"{good}\n{bad_shares}\n{bad_cash}\n"
    with patch("app.services.holding_parser._parse_attempt") as mock_attempt:
        preview = parse(text)
    mock_attempt.assert_not_called()
    assert len(preview.valid_rows) == 1
    assert preview.valid_rows[0].ticker == "AAPL"
    assert preview.valid_rows[0].shares == 10.0
    assert len(preview.issue_rows) == 2
    raws = [r.raw for r in preview.issue_rows]
    assert bad_shares in raws
    assert bad_cash in raws


def test_parse_dialect_keeps_identical_lots() -> None:
    """#92: duplicate lots on dialect re-import are a second lot, never merged."""
    line = "Apple AAPL USD 10 180 IBKR asset_type:stock market:US pricing_mode:auto"
    with patch("app.services.holding_parser._parse_attempt") as mock_attempt:
        preview = parse(f"{line}\n{line}\n")
    mock_attempt.assert_not_called()
    assert len(preview.valid_rows) == 2
    assert preview.valid_rows[0].ticker == preview.valid_rows[1].ticker == "AAPL"


def test_parse_manual_listed_one_numeric_token_as_current_value() -> None:
    """pricing_mode:manual listed row with one number is current_value, not LLM."""
    line = "Family house HOME USD 250000 IBKR asset_type:other market:Other pricing_mode:manual"
    parsed = parse_dialect_line(line)
    assert parsed is not None
    assert parsed["ticker"] == "HOME"
    assert parsed["current_value"] == 250000.0
    assert parsed["shares"] is None
    assert parsed["avg_cost"] is None
    assert parsed["pricing_mode"] == "manual"
    with patch("app.services.holding_parser._parse_attempt") as mock_attempt:
        preview = parse(line)
    mock_attempt.assert_not_called()
    assert len(preview.valid_rows) == 1
    assert preview.valid_rows[0].current_value == 250000.0
    assert preview.valid_rows[0].pricing_mode == "manual"
    assert preview.issue_rows == []


def test_parse_manual_listed_three_numerics_as_shares_avg_cost_current_value() -> None:
    """pricing_mode:manual means three numerics after currency are shares/avg_cost/value."""
    line = (
        "Family office stake HOME USD 10 25000 280000 IBKR "
        "asset_type:other market:Other pricing_mode:manual"
    )
    parsed = parse_dialect_line(line)
    assert parsed is not None
    assert parsed["ticker"] == "HOME"
    assert parsed["shares"] == 10.0
    assert parsed["avg_cost"] == 25000.0
    assert parsed["current_value"] == 280000.0
    assert parsed["pricing_mode"] == "manual"


def test_parse_manual_listed_name_embedding_currency_does_not_steal_fields() -> None:
    """A name like 'My iShares USD 500 Bond ETF' must not bind the first USD."""
    line = (
        "My iShares USD 500 Bond ETF BNDX USD 10 99.5 995 IBKR "
        "asset_type:etf market:US pricing_mode:manual"
    )
    parsed = parse_dialect_line(line)
    assert parsed is not None
    assert parsed["name"] == "My iShares USD 500 Bond ETF"
    assert parsed["ticker"] == "BNDX"
    assert parsed["currency"] == "USD"
    assert parsed["shares"] == 10.0
    assert parsed["avg_cost"] == 99.5
    assert parsed["current_value"] == 995.0
    with patch("app.services.holding_parser._parse_attempt") as mock_attempt:
        preview = parse(line)
    mock_attempt.assert_not_called()
    assert len(preview.valid_rows) == 1
    assert preview.valid_rows[0].name == "My iShares USD 500 Bond ETF"
    assert preview.valid_rows[0].avg_cost == 99.5
    assert preview.issue_rows == []


def test_parse_manual_listed_explicit_placeholder_preserves_missing_avg_cost() -> None:
    """PR #310 round 5: a `-` placeholder in the avg_cost slot must parse back
    to avg_cost=None + current_value preserved — not misread as 'two numbers
    present' (shares/avg_cost) the way blind positional counting would."""
    line = "Family house HOME USD 1 - 250000 IBKR asset_type:other market:Other pricing_mode:manual"
    parsed = parse_dialect_line(line)
    assert parsed is not None
    assert parsed["shares"] == 1.0
    assert parsed["avg_cost"] is None
    assert parsed["current_value"] == 250000.0


def test_parse_manual_listed_explicit_placeholder_preserves_missing_shares() -> None:
    line = (
        "Family office stake HOME USD - 25000 280000 IBKR "
        "asset_type:other market:Other pricing_mode:manual"
    )
    parsed = parse_dialect_line(line)
    assert parsed is not None
    assert parsed["shares"] is None
    assert parsed["avg_cost"] == 25000.0
    assert parsed["current_value"] == 280000.0


def test_parse_export_dialect_skips_llm() -> None:
    text = (
        "##### comment\n"
        "Apple AAPL USD 10 180 IBKR account:IRA asset_type:stock market:US pricing_mode:auto\n"
    )
    with patch("app.services.holding_parser._parse_attempt") as mock_attempt:
        preview = parse(text)
    mock_attempt.assert_not_called()
    assert len(preview.valid_rows) == 1
    assert preview.valid_rows[0].account == "IRA"
