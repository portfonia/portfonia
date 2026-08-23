"""Structural tests locking the B3 identity seam (Ring 1-B design doc §5).

`app/services/**` and `app/tasks/**` must never resolve "who is calling"
from ambient process state (`get_current_user_id()`/`DEV_USER_ID`) — every
identity-bearing call must receive `user_id` as an explicit parameter, with
the one request-scoped exception documented below. This is enforced by
scanning real source text rather than by review attention alone, mirroring
`test_report_assembly.py::test_assembly_module_never_imports_the_shared_cache_models`
(issue #128 A4) — a boundary that only review discipline protects erodes the
first time someone adds a "convenience" call site under time pressure.
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


def test_services_and_tasks_do_not_reference_dev_user_id() -> None:
    """B4: the B3 user_directory.py shim is gone. DEV_USER_ID must not appear
    under app/services/** or app/tasks/** — the bootstrap bind lives in the
    migration and the ops invite `created_by` lives in the admin router."""
    offenders = []
    for path in _py_files(_SERVICES_DIR) + _py_files(_TASKS_DIR):
        source = path.read_text(encoding="utf-8")
        if "DEV_USER_ID" in source:
            offenders.append(str(path))
    assert not offenders, f"DEV_USER_ID referenced in: {offenders}"


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
    """Every route under /holdings, /portfolio, and /reports that needs a
    caller identity must depend on current_principal, not the lower-level
    get_current_user_id — mirrors test_admin_router.py's coverage-by-
    iteration pattern rather than trusting per-endpoint review attention."""
    scoped_prefixes = ("/holdings", "/portfolio", "/reports")
    routes = [
        r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith(scoped_prefixes)
    ]
    assert routes, "expected at least one route under /holdings, /portfolio, /reports"
    offenders = []
    for route in routes:
        dep_calls = {dep.call for dep in route.dependant.dependencies}
        if current_principal not in dep_calls:
            offenders.append(f"{route.methods} {route.path}")
    assert not offenders, f"routes not wired to Depends(current_principal): {offenders}"
