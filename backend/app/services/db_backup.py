"""pg_dump -> OCI Object Storage backup (issue #106).

There is no managed-provider backup net under the self-hosted production
Postgres (decision 2026-08-05) — this is the only safety net. Retention is
enforced entirely by the bucket's Object Lifecycle Policy (a 30-day expiry
on the `daily/` prefix), not by this module — see Obsidian
`Hermes/Portfonia/Portfonia Environment Config.md` for the current policy;
do not copy the retention number here, it would drift out of sync with the
actual bucket config (same trap as the asset_class threshold table once did).

Auth: production runs this on the app VM itself, so the `oci` CLI
authenticates via instance principal (`--auth instance_principal`) — no key
file ever touches the server. This requires the container to reach the
instance metadata service (169.254.169.254); if that's ever blocked (e.g. a
future network-mode change), instance-principal auth fails and the task
alerts via its normal retry-exhaustion path. Local/manual runs (e.g. a
restore drill) fall back to the CLI's default `~/.oci/config` file auth.

Both `pg_dump` and `oci` are invoked as subprocesses, not imported as
Python libraries: `oci`/`oci-cli` pin `cryptography<50.0.0`, which conflicts
with this app's `cryptography==50.0.0` pin (Fernet holdings encryption,
issue #31). Keeping both binaries out of the app's own venv (see
backend/Dockerfile) avoids that conflict entirely.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_OBJECT_PREFIX = "daily"
_DUMP_TIMEOUT_SECONDS = 600
_TRANSFER_TIMEOUT_SECONDS = 300


class BackupError(RuntimeError):
    """pg_dump, or the OCI upload/download step, failed."""


def _oci_auth_args() -> list[str]:
    settings = get_settings()
    if settings.APP_ENV == "production":
        return ["--auth", "instance_principal"]
    return []


def build_object_name(db_name: str, when: datetime | None = None) -> str:
    when = when or datetime.now(UTC)
    return f"{_OBJECT_PREFIX}/{db_name}-{when.strftime('%Y%m%d-%H%M%S')}.dump"


def run_pg_dump(dest_path: Path) -> None:
    """Dump the configured database to dest_path in pg_restore-compatible
    custom format (-Fc — already compressed, no separate gzip step needed)."""
    settings = get_settings()
    cmd = [
        "pg_dump",
        "-h",
        settings.DB_HOST,
        "-p",
        str(settings.DB_PORT),
        "-U",
        settings.DB_USER,
        "-d",
        settings.DB_NAME,
        "-Fc",
        "-w",
        "-f",
        str(dest_path),
    ]
    env = {**os.environ, "PGPASSWORD": settings.DB_PASSWORD.get_secret_value()}
    result = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=_DUMP_TIMEOUT_SECONDS
    )
    if result.returncode != 0:
        raise BackupError(f"pg_dump exited {result.returncode}: {result.stderr.strip()}")


def upload_object(local_path: Path, object_name: str) -> None:
    settings = get_settings()
    cmd = [
        "oci",
        "os",
        "object",
        "put",
        "--namespace",
        settings.BACKUP_OCI_NAMESPACE,
        "--bucket-name",
        settings.BACKUP_OCI_BUCKET,
        "--name",
        object_name,
        "--file",
        str(local_path),
        "--force",
        *_oci_auth_args(),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=_TRANSFER_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise BackupError(f"oci upload exited {result.returncode}: {result.stderr.strip()}")


def download_object(object_name: str, dest_path: Path) -> None:
    """Fetch a backup object back to disk. Used only by the manual
    restore-drill procedure (Obsidian runbook) — never by the scheduled task."""
    settings = get_settings()
    cmd = [
        "oci",
        "os",
        "object",
        "get",
        "--namespace",
        settings.BACKUP_OCI_NAMESPACE,
        "--bucket-name",
        settings.BACKUP_OCI_BUCKET,
        "--name",
        object_name,
        "--file",
        str(dest_path),
        *_oci_auth_args(),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=_TRANSFER_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise BackupError(f"oci download exited {result.returncode}: {result.stderr.strip()}")


def backup_database() -> str | None:
    """Dump the configured database and upload it. Returns the uploaded
    object name, or None if backups are disabled (BACKUP_OCI_NAMESPACE unset
    — the default for local dev, so a locally-started Beat never uploads dev
    dumps anywhere).

    In production, an unset namespace is NOT a silent no-op: this is the only
    DB restore safety net after dropping managed Postgres backups
    (2026-08-05 decision), so a misconfigured/missing env var must fail loudly
    (and reach backup_tasks.py's ops-alert path) rather than report a daily
    "success" that never actually backed anything up."""
    settings = get_settings()
    if not settings.BACKUP_OCI_NAMESPACE:
        if settings.APP_ENV == "production":
            raise BackupError(
                "BACKUP_OCI_NAMESPACE is unset in production — refusing to "
                "silently skip the only DB backup safety net"
            )
        logger.info("backup_database: BACKUP_OCI_NAMESPACE unset, backups disabled — skipping")
        return None

    object_name = build_object_name(settings.DB_NAME)
    with tempfile.TemporaryDirectory() as tmp:
        dump_path = Path(tmp) / "backup.dump"
        run_pg_dump(dump_path)
        size = dump_path.stat().st_size
        if size == 0:
            raise BackupError("pg_dump produced an empty file")
        upload_object(dump_path, object_name)

    logger.info("backup_database: uploaded %s (%d bytes)", object_name, size)
    return object_name
