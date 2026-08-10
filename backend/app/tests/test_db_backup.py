"""Tests for the pg_dump -> OCI Object Storage backup service (issue #106).

subprocess.run is mocked throughout — these tests never shell out to a real
pg_dump or oci binary. The one thing that's real is disk I/O against a temp
dir, so the "empty dump" guard can be exercised honestly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_build_object_name_uses_daily_prefix_and_timestamp() -> None:
    from app.services.db_backup import build_object_name

    when = datetime(2026, 8, 10, 3, 0, 0, tzinfo=UTC)
    name = build_object_name("portfonia_prod", when=when)
    assert name == "daily/portfonia_prod-20260810-030000.dump"


@patch("app.services.db_backup.subprocess.run")
def test_run_pg_dump_raises_on_nonzero_exit(mock_run: MagicMock, tmp_path: Path) -> None:
    from app.services.db_backup import BackupError, run_pg_dump

    mock_run.return_value = MagicMock(returncode=1, stderr="connection refused")
    with pytest.raises(BackupError, match="connection refused"):
        run_pg_dump(tmp_path / "backup.dump")


@patch("app.services.db_backup.subprocess.run")
def test_run_pg_dump_passes_password_via_env_not_argv(mock_run: MagicMock, tmp_path: Path) -> None:
    from app.services.db_backup import run_pg_dump

    mock_run.return_value = MagicMock(returncode=0, stderr="")
    run_pg_dump(tmp_path / "backup.dump")

    _, kwargs = mock_run.call_args
    cmd = mock_run.call_args.args[0]
    assert not any("PGPASSWORD" in part or "password" in part.lower() for part in cmd)
    assert "PGPASSWORD" in kwargs["env"]


@patch("app.services.db_backup.subprocess.run")
def test_run_pg_dump_disables_interactive_password_prompt(
    mock_run: MagicMock, tmp_path: Path
) -> None:
    """-w / --no-password: a bad PGPASSWORD must fail fast with a clear error,
    not hang on an interactive prompt until the subprocess timeout fires."""
    from app.services.db_backup import run_pg_dump

    mock_run.return_value = MagicMock(returncode=0, stderr="")
    run_pg_dump(tmp_path / "backup.dump")

    cmd = mock_run.call_args.args[0]
    assert "-w" in cmd


@patch("app.services.db_backup.subprocess.run")
def test_upload_object_raises_on_nonzero_exit(mock_run: MagicMock, tmp_path: Path) -> None:
    from app.services.db_backup import BackupError, upload_object

    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"fake dump content")
    mock_run.return_value = MagicMock(returncode=1, stderr="NotAuthorized")
    with pytest.raises(BackupError, match="NotAuthorized"):
        upload_object(dump, "daily/portfonia_prod-20260810-030000.dump")


@patch("app.services.db_backup.subprocess.run")
def test_upload_object_uses_instance_principal_in_production(
    mock_run: MagicMock, tmp_path: Path
) -> None:
    from app.core.config import get_settings
    from app.services.db_backup import upload_object

    get_settings.cache_clear()
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"fake dump content")
    mock_run.return_value = MagicMock(returncode=0, stderr="")

    with patch.dict("os.environ", {"APP_ENV": "production"}):
        get_settings.cache_clear()
        try:
            upload_object(dump, "daily/x.dump")
        finally:
            get_settings.cache_clear()

    cmd = mock_run.call_args.args[0]
    assert "--auth" in cmd
    assert cmd[cmd.index("--auth") + 1] == "instance_principal"


@patch("app.services.db_backup.subprocess.run")
def test_upload_object_omits_auth_flag_outside_production(
    mock_run: MagicMock, tmp_path: Path
) -> None:
    from app.core.config import get_settings
    from app.services.db_backup import upload_object

    get_settings.cache_clear()
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"fake dump content")
    mock_run.return_value = MagicMock(returncode=0, stderr="")

    upload_object(dump, "daily/x.dump")

    cmd = mock_run.call_args.args[0]
    assert "--auth" not in cmd


@patch("app.services.db_backup.upload_object")
@patch("app.services.db_backup.run_pg_dump")
def test_backup_database_skips_when_namespace_unset(
    mock_dump: MagicMock, mock_upload: MagicMock
) -> None:
    """Silent skip is the correct behavior for non-production (local dev
    default) — see the next test for the opposite requirement in production."""
    from app.core.config import get_settings
    from app.services.db_backup import backup_database

    get_settings.cache_clear()
    with patch.dict("os.environ", {"BACKUP_OCI_NAMESPACE": "", "APP_ENV": "development"}):
        get_settings.cache_clear()
        try:
            result = backup_database()
        finally:
            get_settings.cache_clear()

    assert result is None
    mock_dump.assert_not_called()
    mock_upload.assert_not_called()


@patch("app.services.db_backup.upload_object")
@patch("app.services.db_backup.run_pg_dump")
def test_backup_database_raises_in_production_when_namespace_unset(
    mock_dump: MagicMock, mock_upload: MagicMock
) -> None:
    """A missing BACKUP_OCI_NAMESPACE in production must fail loudly, not
    return a silent "success" — this is the only DB restore safety net after
    dropping managed Postgres backups (2026-08-05 decision)."""
    from app.core.config import get_settings
    from app.services.db_backup import BackupError, backup_database

    get_settings.cache_clear()
    with patch.dict("os.environ", {"BACKUP_OCI_NAMESPACE": "", "APP_ENV": "production"}):
        get_settings.cache_clear()
        try:
            with pytest.raises(BackupError, match="BACKUP_OCI_NAMESPACE"):
                backup_database()
        finally:
            get_settings.cache_clear()

    mock_dump.assert_not_called()
    mock_upload.assert_not_called()


@patch("app.services.db_backup.upload_object")
@patch("app.services.db_backup.run_pg_dump")
def test_backup_database_uploads_and_returns_object_name(
    mock_dump: MagicMock, mock_upload: MagicMock
) -> None:
    from app.core.config import get_settings
    from app.services.db_backup import backup_database

    def fake_dump(dest_path: Path) -> None:
        dest_path.write_bytes(b"fake dump content")

    mock_dump.side_effect = fake_dump

    get_settings.cache_clear()
    with patch.dict("os.environ", {"BACKUP_OCI_NAMESPACE": "test-oci-namespace"}):
        get_settings.cache_clear()
        try:
            result = backup_database()
        finally:
            get_settings.cache_clear()

    assert result is not None
    assert result.startswith("daily/")
    mock_upload.assert_called_once()


@patch("app.services.db_backup.run_pg_dump")
def test_backup_database_raises_on_empty_dump(mock_dump: MagicMock) -> None:
    from app.core.config import get_settings
    from app.services.db_backup import BackupError, backup_database

    def fake_empty_dump(dest_path: Path) -> None:
        dest_path.write_bytes(b"")

    mock_dump.side_effect = fake_empty_dump

    get_settings.cache_clear()
    with patch.dict("os.environ", {"BACKUP_OCI_NAMESPACE": "test-oci-namespace"}):
        get_settings.cache_clear()
        try:
            with pytest.raises(BackupError, match="empty"):
                backup_database()
        finally:
            get_settings.cache_clear()


@patch("app.services.db_backup.subprocess.run")
def test_download_object_raises_on_nonzero_exit(mock_run: MagicMock, tmp_path: Path) -> None:
    from app.services.db_backup import BackupError, download_object

    mock_run.return_value = MagicMock(returncode=1, stderr="ObjectNotFound")
    with pytest.raises(BackupError, match="ObjectNotFound"):
        download_object("daily/missing.dump", tmp_path / "out.dump")
