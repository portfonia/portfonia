"""Tests for GitHub auto-issue creation (ops alerting companion, issue #195)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

# GitHub's documented issue-body ceiling. create_bug_report must stay under it
# even when the caller interpolates a huge exception string (issue #195).
_GITHUB_ISSUE_BODY_MAX = 65_536


class _FakeResp:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return {"html_url": "https://github.com/portfonia/portfonia/issues/1"}


def _patch_github_post(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    class _FakeClient:
        def __init__(self, timeout: float | None = None) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            json: dict[str, object] | None = None,
        ) -> _FakeResp:
            captured["url"] = url
            captured["payload"] = json
            return _FakeResp()

    monkeypatch.setattr("app.services.github_issues.httpx.Client", _FakeClient)
    settings = MagicMock()
    settings.GITHUB_TOKEN = SecretStr("test-token")
    settings.GITHUB_REPO = "portfonia/portfonia"
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)


def test_create_bug_report_truncates_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.github_issues import create_bug_report

    captured: dict[str, object] = {}
    _patch_github_post(monkeypatch, captured)

    huge = "x" * (_GITHUB_ISSUE_BODY_MAX + 10_000)
    url = create_bug_report(title="capture failure: backfill_ohlcv_task", body=huge)

    assert url == "https://github.com/portfonia/portfonia/issues/1"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    body = payload["body"]
    assert isinstance(body, str)
    assert len(body) <= _GITHUB_ISSUE_BODY_MAX
    assert body.endswith("...(truncated)")


def test_capture_failed_truncates_huge_exception_in_github_body() -> None:
    from app.tasks.capture_tasks import _capture_failed

    huge_sql = "INSERT INTO price_snapshots " + ("x" * 80_000)
    with (
        patch("app.tasks.capture_tasks.send_ops_alert") as mock_alert,
        patch("app.tasks.capture_tasks.create_bug_report") as mock_issue,
    ):
        _capture_failed("backfill_ohlcv_task", RuntimeError(huge_sql))

    issue_body = mock_issue.call_args.kwargs["body"]
    alert_body = mock_alert.call_args.kwargs["body"]
    assert len(issue_body) < 20_000
    assert len(alert_body) < 20_000
    assert "truncated" in issue_body
    assert "truncated" in alert_body
    assert "backfill_ohlcv_task" in issue_body


def test_format_exc_redacts_bound_sql_parameters() -> None:
    """Compiled INSERT text can carry ticker/fund_code bindings (Concept §8.8)."""
    from app.tasks.capture_tasks import _format_exc

    text = _format_exc(
        RuntimeError(
            "INSERT INTO price_snapshots ... "
            "[parameters: {'ticker_m0': '513100', 'market_m0': 'A-Share'}]"
        )
    )
    assert "513100" not in text
    assert "redacted" in text
