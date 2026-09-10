"""Typed Eastmoney NAV-history outcomes and Sina latest-NAV capture validation (#389)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import httpx

from app.services.capture_results import NavPoint
from app.services.fund_nav_fetcher import (
    fetch_nav_history,
    fetch_nav_history_outcome,
    sina_latest_nav_point,
)


def _response(
    *,
    json_payload: object | None = None,
    text: str = "",
    status_code: int = 200,
    json_error: Exception | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if status_code >= 400:
        err = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=MagicMock(status_code=status_code)
        )
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status.return_value = None
    if json_error is not None:
        resp.json.side_effect = json_error
    else:
        resp.json.return_value = json_payload
    return resp


def _client(resp: MagicMock | Exception) -> MagicMock:
    client = MagicMock()
    if isinstance(resp, Exception):
        client.get.side_effect = resp
    else:
        client.get.return_value = resp
    return client


def test_html_http_200_is_parse_error() -> None:
    client = _client(_response(text="<html>blocked</html>", json_error=ValueError("not json")))
    out = fetch_nav_history_outcome(
        "019547",
        client,
        start_date=date(2026, 8, 8),
        end_date=date(2026, 9, 7),
        as_of_date=date(2026, 9, 8),
    )
    assert out.points == ()
    assert out.error == "parse"


def test_nonzero_errcode_is_provider_error() -> None:
    client = _client(_response(json_payload={"ErrCode": 500, "Data": {"LSJZList": []}}))
    out = fetch_nav_history_outcome(
        "019547",
        client,
        start_date=date(2026, 8, 8),
        end_date=date(2026, 9, 7),
        as_of_date=date(2026, 9, 8),
    )
    assert out.points == ()
    assert out.error == "provider_error"


def test_valid_empty_history_is_success_not_an_exception() -> None:
    client = _client(_response(json_payload={"ErrCode": 0, "Data": {"LSJZList": []}}))
    out = fetch_nav_history_outcome(
        "019547",
        client,
        start_date=date(2026, 8, 8),
        end_date=date(2026, 9, 7),
        as_of_date=date(2026, 9, 8),
    )
    assert out.points == ()
    assert out.error is None


def test_transport_error_is_distinct_from_http() -> None:
    transport = _client(httpx.ConnectError("down"))
    http_err = _client(_response(status_code=503, text="unavailable"))
    assert (
        fetch_nav_history_outcome(
            "019547",
            transport,
            start_date=date(2026, 8, 8),
            end_date=date(2026, 9, 7),
            as_of_date=date(2026, 9, 8),
        ).error
        == "transport"
    )
    assert (
        fetch_nav_history_outcome(
            "019547",
            http_err,
            start_date=date(2026, 8, 8),
            end_date=date(2026, 9, 7),
            as_of_date=date(2026, 9, 8),
        ).error
        == "http"
    )


def test_uses_unit_nav_dwjz_not_accumulated_ljjz() -> None:
    client = _client(
        _response(
            json_payload={
                "ErrCode": 0,
                "Data": {
                    "LSJZList": [
                        {"FSRQ": "2026-09-07", "DWJZ": "1.5765", "LJJZ": "9.9999"},
                    ]
                },
            }
        )
    )
    out = fetch_nav_history_outcome(
        "019547",
        client,
        start_date=date(2026, 8, 8),
        end_date=date(2026, 9, 7),
        as_of_date=date(2026, 9, 8),
    )
    assert out.error is None
    assert out.points == (NavPoint(date(2026, 9, 7), Decimal("1.5765"), "eastmoney"),)


def test_rejects_zero_nan_future_and_out_of_bounds_rows() -> None:
    client = _client(
        _response(
            json_payload={
                "ErrCode": 0,
                "Data": {
                    "LSJZList": [
                        {"FSRQ": "2026-09-07", "DWJZ": "0"},
                        {"FSRQ": "2026-09-06", "DWJZ": "NaN"},
                        {"FSRQ": "2026-09-09", "DWJZ": "1.2"},
                        {"FSRQ": "2026-07-01", "DWJZ": "1.2"},
                        {"FSRQ": "2026-09-04", "DWJZ": "1.5"},
                    ]
                },
            }
        )
    )
    out = fetch_nav_history_outcome(
        "019547",
        client,
        start_date=date(2026, 8, 8),
        end_date=date(2026, 9, 7),
        as_of_date=date(2026, 9, 8),
    )
    assert out.error is None
    assert out.points == (NavPoint(date(2026, 9, 4), Decimal("1.5"), "eastmoney"),)


def test_conflicting_duplicate_dates_drop_that_date() -> None:
    client = _client(
        _response(
            json_payload={
                "ErrCode": 0,
                "Data": {
                    "LSJZList": [
                        {"FSRQ": "2026-09-07", "DWJZ": "1.5765"},
                        {"FSRQ": "2026-09-07", "DWJZ": "1.5800"},
                        {"FSRQ": "2026-09-04", "DWJZ": "1.50"},
                        {"FSRQ": "2026-09-04", "DWJZ": "1.50"},
                    ]
                },
            }
        )
    )
    out = fetch_nav_history_outcome(
        "019547",
        client,
        start_date=date(2026, 8, 8),
        end_date=date(2026, 9, 7),
        as_of_date=date(2026, 9, 8),
    )
    assert out.points == (NavPoint(date(2026, 9, 4), Decimal("1.50"), "eastmoney"),)


def test_list_wrapper_still_swallows_errors() -> None:
    client = _client(_response(text="<html>blocked</html>", json_error=ValueError("not json")))
    assert fetch_nav_history("019547", client) == []


def test_sina_uses_unit_nav_field_not_accumulated() -> None:
    client = MagicMock()
    body = 'var hq_str_f_019547="example,1.5765,1.5765,9.9999,2026-09-07";'
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.content = body.encode("gbk")
    client.get.return_value = resp
    point = sina_latest_nav_point("019547", client, as_of_date=date(2026, 9, 8))
    assert point == NavPoint(date(2026, 9, 7), Decimal("1.5765"), "sina")


def test_sina_rejects_placeholder_nonfinite_zero_future_and_wrong_key() -> None:
    cases = [
        'var hq_str_f_019547="example,--,--,--,2026-09-07";',
        'var hq_str_f_019547="example,NaN,NaN,NaN,2026-09-07";',
        'var hq_str_f_019547="example,Infinity,1,1,2026-09-07";',
        'var hq_str_f_019547="example,0,0,0,2026-09-07";',
        'var hq_str_f_019547="example,-1.2,1,1,2026-09-07";',
        'var hq_str_f_019547="example,1.5765,1.5765,1.5765,2026-09-09";',
        'var hq_str_f_008142="example,1.5765,1.5765,1.5765,2026-09-07";',
    ]
    for body in cases:
        client = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.content = body.encode("gbk")
        client.get.return_value = resp
        assert sina_latest_nav_point("019547", client, as_of_date=date(2026, 9, 8)) is None
