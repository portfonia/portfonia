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
