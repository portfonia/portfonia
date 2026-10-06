"""Structural tests locking the B3 identity seam (Ring 1-B design doc §5).

Services receive caller and system identities explicitly. Tasks may resolve
ADMIN_ID as a non-account system creator, never as ambient caller identity.
Source scans enforce the retired DEV_USER_ID removal and the service/router
boundary, alongside the request-scoped current_principal checks below.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.routing import APIRoute

from app.core.deps import current_principal
from app.main import app

_SERVICES_DIR = Path(__file__).resolve().parent.parent / "services"
_TASKS_DIR = Path(__file__).resolve().parent.parent / "tasks"


def _py_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def test_services_and_tasks_do_not_call_get_current_user_id() -> None:
    """No ambient identity resolution anywhere under app/services/** or
    app/tasks/** — every caller must receive user_id explicitly."""
    offenders = []
    for path in _py_files(_SERVICES_DIR) + _py_files(_TASKS_DIR):
        source = path.read_text(encoding="utf-8")
        if "get_current_user_id" in source:
            offenders.append(str(path))
    assert not offenders, f"get_current_user_id referenced in: {offenders}"


def test_app_does_not_reference_dev_user_id() -> None:
    """The retired caller identity is absent outside test fixtures."""
    app_dir = _SERVICES_DIR.parent
    offenders = [
        str(p)
        for p in _py_files(app_dir)
        if "tests" not in p.parts and "DEV_USER_ID" in p.read_text()
    ]
    assert not offenders, f"DEV_USER_ID referenced in: {offenders}"


def test_services_receive_explicit_system_actor() -> None:
    """Tasks may resolve the root system actor; services receive it explicitly."""
    offenders = [
        str(p)
        for p in _py_files(_SERVICES_DIR)
        if "ADMIN_ID" in p.read_text() or "from app.routers" in p.read_text()
    ]
    assert not offenders, f"ambient actor or router import in services: {offenders}"


def test_is_admin_is_never_read_outside_the_model() -> None:
    """Decision point 12: users.is_admin is a reserved column. Ring 1 must
    not consult it — the ops channel is ADMIN_API_TOKEN, not this flag."""
    app_dir = Path(__file__).resolve().parent.parent
    model = app_dir / "models" / "user.py"
    offenders = []
    for path in _py_files(app_dir):
        if path == model or "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if ".is_admin" in source or "User.is_admin" in source:
            offenders.append(str(path.relative_to(app_dir)))
    assert not offenders, f"is_admin read in: {offenders}"


# ---------------------------------------------------------------------------
# PR #181 review: current_principal must be the ONE request-scoped identity
# entry point, not just the one reports.py happens to use. A route still
# wired to Depends(get_current_user_id) directly gets `dependency_overrides`
# for free (get_current_user_id is itself a Depends target), but it does
# NOT follow B4's JWT swap — that swap lands entirely inside
# current_principal's body, so any route bypassing it keeps serving
# DEV_USER_ID forever, a split-identity leak between routers.
# ---------------------------------------------------------------------------


def test_every_identity_bearing_route_depends_on_current_principal() -> None:
    """Every route under /holdings, /portfolio, /reports, and
    /investment-context that needs a caller identity must depend on
    current_principal, not the lower-level get_current_user_id — mirrors
    test_admin_router.py's coverage-by-iteration pattern rather than
    trusting per-endpoint review attention (PR #212 review finding: a new
    identity-scoped router must be added here, not just wired correctly)."""
    scoped_prefixes = ("/holdings", "/portfolio", "/reports", "/investment-context", "/me")
    routes = [
        r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith(scoped_prefixes)
    ]
    assert routes, (
        "expected at least one route under /holdings, /portfolio, /reports, /investment-context"
    )
    offenders = []
    for route in routes:
        dep_calls = {dep.call for dep in route.dependant.dependencies}
        if current_principal not in dep_calls:
            offenders.append(f"{route.methods} {route.path}")
    assert not offenders, f"routes not wired to Depends(current_principal): {offenders}"
