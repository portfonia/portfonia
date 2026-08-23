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

# `user_directory.py` is the one deliberate, temporary exception (Ring 1-B
# design doc §5.3): a new B3-era shim that resolves DEV_USER_ID to
# DEV_USER_EMAIL given an explicitly-passed user_id — not ambient
# resolution, a lookup. B4 deletes this file's DEV_USER_ID reference when
# `users` replaces it; nothing else in app/services/** or app/tasks/** gets
# the same pass.
_DEV_USER_ID_SHIM = _SERVICES_DIR / "user_directory.py"


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


def test_services_and_tasks_do_not_reference_dev_user_id_except_the_shim() -> None:
    """DEV_USER_ID may appear in exactly one file: the documented B3->B4
    shim. Anywhere else, it's the same ambient-identity antipattern with a
    different name."""
    offenders = []
    for path in _py_files(_SERVICES_DIR) + _py_files(_TASKS_DIR):
        if path == _DEV_USER_ID_SHIM:
            continue
        source = path.read_text(encoding="utf-8")
        if "DEV_USER_ID" in source:
            offenders.append(str(path))
    assert not offenders, f"DEV_USER_ID referenced outside user_directory.py in: {offenders}"


def test_dev_user_id_shim_file_still_exists_and_actually_uses_it() -> None:
    """Guards against the exception list above going stale: if
    user_directory.py stops referencing DEV_USER_ID (e.g. once B4 lands),
    this test should fail and force removing it from the carve-out too."""
    assert _DEV_USER_ID_SHIM.exists()
    assert "DEV_USER_ID" in _DEV_USER_ID_SHIM.read_text(encoding="utf-8")


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
