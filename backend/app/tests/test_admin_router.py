"""Integration tests for /admin/* (issue #128 Ring 1 stage B, checkpoint B2).

Covers: token auth end-to-end through the real FastAPI dependency chain,
the structural guarantee that every /admin route is protected (B-UAT-4), the
moved POST /admin/portfolio/refresh endpoint (decision point 8/11), audit
logging that never records the token, and the repeated-401 ops alert.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.routers.admin as admin_module
from app.core.config import get_settings
from app.core.deps import require_ops_token
from app.main import app
from app.services.fund_nav_fetcher import FundNavFetchResult
from app.services.fx_fetcher import FxFetchResult
from app.services.price_fetcher import PriceFetchResult


def _headers(token: str | None = None) -> dict[str, str]:
    tok = token if token is not None else get_settings().ADMIN_API_TOKEN.get_secret_value()
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(autouse=True)
def _reset_consecutive_401_counter() -> None:
    """The counter is module-level state shared across the whole test session —
    each test must start from zero, or an earlier test's 401s bleed in."""
    admin_module._consecutive_401_count = 0


@contextmanager
def _patched_fetchers_noop() -> Iterator[None]:
    """Patches all four refresh sub-fetchers to their zero-effect default —
    for tests where the point is auth/logging/alerting, not the field
    mapping (test_refresh_accepts_correct_token covers that separately with
    real values)."""
    with (
        patch(
            "app.routers.admin.price_fetcher.update_holding_prices",
            return_value=PriceFetchResult(updated=0, failed=[]),
        ),
        patch("app.routers.admin.price_fetcher.backfill_sectors", return_value=0),
        patch(
            "app.routers.admin.update_fund_navs",
            return_value=FundNavFetchResult(updated=0, failed=[]),
        ),
        patch(
            "app.routers.admin.fx_fetcher.update_fx_rates",
            return_value=FxFetchResult(upserted=0, failed=[]),
        ),
    ):
        yield


def test_refresh_requires_token(app_client: TestClient) -> None:
    resp = app_client.post("/admin/portfolio/refresh")
    assert resp.status_code == 401


def test_refresh_rejects_wrong_token(app_client: TestClient) -> None:
    resp = app_client.post("/admin/portfolio/refresh", headers=_headers("wrong-token"))
    assert resp.status_code == 401


def test_non_ascii_bearer_header_returns_401_not_500(app_client: TestClient) -> None:
    """A raw non-ASCII byte in the header must not become an unhandled
    exception -> 500 (PR #177 review round 2: `secrets.compare_digest`
    raises `TypeError` on non-ASCII str pairs). httpx's TestClient refuses
    to encode a non-ASCII `str` header value client-side, so the raw-bytes
    header form is used here to actually exercise what a real HTTP request
    could carry."""
    resp = app_client.post(
        "/admin/portfolio/refresh",
        headers=[(b"authorization", b"Bearer caf\xe9")],
    )
    assert resp.status_code == 401


def test_non_ascii_bearer_header_counts_as_401_not_a_reset(app_client: TestClient) -> None:
    """The same bug let an attacker dodge the anti-flood alert: a 500 took
    the "else" branch in AdminLoggingRoute and reset the consecutive-401
    counter. A non-ASCII credential must count as an ordinary 401 instead."""
    threshold = admin_module._CONSECUTIVE_401_ALERT_THRESHOLD
    for _ in range(threshold - 1):
        app_client.post("/admin/portfolio/refresh", headers=_headers("bad-token"))

    resp = app_client.post(
        "/admin/portfolio/refresh",
        headers=[(b"authorization", b"Bearer caf\xe9")],
    )

    assert resp.status_code == 401
    assert admin_module._consecutive_401_count == threshold


def test_refresh_accepts_correct_token(app_client: TestClient, db_session: Session) -> None:
    with (
        patch(
            "app.routers.admin.price_fetcher.update_holding_prices",
            return_value=PriceFetchResult(updated=1, failed=[]),
        ),
        patch("app.routers.admin.price_fetcher.backfill_sectors", return_value=2),
        patch(
            "app.routers.admin.update_fund_navs",
            return_value=FundNavFetchResult(updated=1, failed=["999"]),
        ),
        patch(
            "app.routers.admin.fx_fetcher.update_fx_rates",
            return_value=FxFetchResult(upserted=2, failed=[]),
        ),
    ):
        resp = app_client.post("/admin/portfolio/refresh", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "prices_updated": 1,
        "prices_failed": [],
        "sectors_backfilled": 2,
        "funds_updated": 1,
        "funds_failed": ["999"],
        "fx_upserted": 2,
        "fx_failed": [],
    }


def test_refresh_accepts_prev_token_during_rotation(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import SecretStr

    settings = get_settings()
    old_token = settings.ADMIN_API_TOKEN.get_secret_value()
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", SecretStr("rotated-in-token"))
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN_PREV", SecretStr(old_token))

    with _patched_fetchers_noop():
        resp = app_client.post("/admin/portfolio/refresh", headers=_headers(old_token))

    assert resp.status_code == 200


def test_old_portfolio_refresh_route_removed(app_client: TestClient) -> None:
    """Ordinary users must not be able to trigger a global market-data refresh
    any more — decision point 8, now enforced via the ops token channel instead."""
    resp = app_client.post("/portfolio/refresh")
    assert resp.status_code == 404


def test_all_admin_routes_require_ops_token() -> None:
    """B-UAT-4: router-level dependency coverage, not per-endpoint — a new
    /admin endpoint that forgets to opt in should be structurally impossible."""
    admin_routes = [
        r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/admin")
    ]
    assert admin_routes, "expected at least one /admin route to exist"
    for route in admin_routes:
        dep_calls = {dep.call for dep in route.dependant.dependencies}
        assert require_ops_token in dep_calls, f"{route.path} is missing require_ops_token"


def test_admin_call_is_audit_logged_without_the_token(
    app_client: TestClient, db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    token = get_settings().ADMIN_API_TOKEN.get_secret_value()
    # alembic/env.py's fileConfig() (run once per session by session_test_db)
    # disables any already-instantiated logger, including app.core.ops_log's
    # module-level logger created at import time — see CLAUDE.md's Tests
    # section ("caplog assertions on an already-imported module's logger").
    logging.getLogger("app.core.ops_log").disabled = False
    with caplog.at_level(logging.INFO, logger="app.core.ops_log"), _patched_fetchers_noop():
        resp = app_client.post("/admin/portfolio/refresh", headers=_headers(token))

    assert resp.status_code == 200
    audit_lines = [r.message for r in caplog.records if "OPS_EVENT" in r.message]
    assert any("/admin/portfolio/refresh" in line for line in audit_lines)
    assert any("200" in line for line in audit_lines)
    assert token not in caplog.text


def test_repeated_401_triggers_one_ops_alert(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    alert_mock = MagicMock()
    monkeypatch.setattr("app.routers.admin.send_admin_alert_task.delay", alert_mock)

    threshold = admin_module._CONSECUTIVE_401_ALERT_THRESHOLD
    for _ in range(threshold - 1):
        app_client.post("/admin/portfolio/refresh", headers=_headers("bad-token"))
    alert_mock.assert_not_called()

    app_client.post("/admin/portfolio/refresh", headers=_headers("bad-token"))
    alert_mock.assert_called_once()

    # Discriminating check (Grok review round 1, PR #177): the assertions above
    # alone can't tell "alert once per threshold, then quiet" (the `%` in the
    # production code) apart from "alert on every request from the threshold
    # onward" (a `>=` bug) — both produce exactly one call by request #5. A
    # 6th and 9th bad request must NOT add a second alert; only the 10th would.
    for _ in range(threshold - 1):
        app_client.post("/admin/portfolio/refresh", headers=_headers("bad-token"))
    alert_mock.assert_called_once()

    token = get_settings().ADMIN_API_TOKEN.get_secret_value()
    assert token not in str(alert_mock.call_args)


def test_alert_is_enqueued_not_called_directly(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #177 review round 3: send_ops_alert makes a blocking 15s-timeout
    HTTP call. It must never run inline on the request's event loop — only
    .delay() (a fast enqueue) is allowed here; the real send happens in a
    separate Celery worker (app/tasks/admin_tasks.py)."""
    direct_call_mock = MagicMock()
    monkeypatch.setattr("app.tasks.admin_tasks.send_ops_alert", direct_call_mock)
    delay_mock = MagicMock()
    monkeypatch.setattr("app.routers.admin.send_admin_alert_task.delay", delay_mock)

    threshold = admin_module._CONSECUTIVE_401_ALERT_THRESHOLD
    for _ in range(threshold):
        app_client.post("/admin/portfolio/refresh", headers=_headers("bad-token"))

    delay_mock.assert_called_once()
    direct_call_mock.assert_not_called()


def test_success_resets_consecutive_401_counter(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    alert_mock = MagicMock()
    monkeypatch.setattr("app.routers.admin.send_admin_alert_task.delay", alert_mock)

    threshold = admin_module._CONSECUTIVE_401_ALERT_THRESHOLD
    for _ in range(threshold - 1):
        app_client.post("/admin/portfolio/refresh", headers=_headers("bad-token"))

    with _patched_fetchers_noop():
        resp = app_client.post("/admin/portfolio/refresh", headers=_headers())
    # The reset logic fires on any non-401 status — a real bug that made this
    # call fail some other way (e.g. 422/500) would still zero the counter,
    # so this must assert the call was an actual success, not just non-401
    # (Grok review round 1, PR #177: reproduced by injecting a 422 here and
    # confirming this test still passed without this assertion).
    assert resp.status_code == 200

    for _ in range(threshold - 1):
        app_client.post("/admin/portfolio/refresh", headers=_headers("bad-token"))
    alert_mock.assert_not_called()
