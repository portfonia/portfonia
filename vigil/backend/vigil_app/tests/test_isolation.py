"""P1.1-A02: importing either application does not register the other."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_VIGIL_BACKEND = _REPO_ROOT / "vigil" / "backend"
_PORTFONIA_BACKEND = _REPO_ROOT / "backend"


def _run(code: str, pythonpath: Path, extra_env: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(pythonpath)
    if extra_env:
        env.update(extra_env)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(pythonpath),
        env=env,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"subprocess failed ({completed.returncode}):\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout


def test_importing_vigil_does_not_register_portfonia_tasks(required_env: dict[str, str]) -> None:
    code = """
from vigil_app.tasks import celery_app
include = list(celery_app.conf.include or [])
names = [n for n in celery_app.tasks if not n.startswith("celery.")]
assert include, "Vigil Celery include list is empty"
assert names, "Vigil registered no application tasks"
assert all(item.startswith("vigil_app.tasks.") for item in include)
assert all(name.startswith("vigil_app.tasks.") for name in names)
print("ok")
"""
    stdout = _run(code, _VIGIL_BACKEND, extra_env=required_env)
    assert "ok" in stdout


def test_importing_portfonia_does_not_register_vigil_tasks() -> None:
    code = """
from app.tasks import celery_app
include = list(celery_app.conf.include or [])
assert include, "Portfonia Celery include list is empty"
assert all(item.startswith("app.tasks.") for item in include)
assert not any(item.startswith("vigil_app.") for item in include)
print("ok")
"""
    stdout = _run(code, _PORTFONIA_BACKEND)
    assert "ok" in stdout


def test_vigil_does_not_import_portfonia_database(required_env: dict[str, str]) -> None:
    code = """
import sys
import vigil_app.core.database
import vigil_app.models
assert "app.core.database" not in sys.modules
assert "app.models.base" not in sys.modules
from vigil_app.models.base import Base
assert "vaults" in Base.metadata.tables
assert "holdings" not in Base.metadata.tables
print("ok")
"""
    stdout = _run(code, _VIGIL_BACKEND, extra_env=required_env)
    assert "ok" in stdout
