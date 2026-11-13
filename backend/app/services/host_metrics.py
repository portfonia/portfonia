"""Host-only Linux metrics and daily Ops alerting (#886)."""

import os
from datetime import datetime, timedelta
from pathlib import Path

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.alert_dedup import already_alerted, mark_alerted
from app.core.config import get_settings
from app.core.timezones import ET
from app.models.usage import HostSample
from app.services.email_sender import send_ops_alert

PROC_ROOT = Path("/proc")
STATVFS_PATH = "/"
PAGE_BYTES = 4096
DISK_THRESHOLD = 0.80
MEMORY_THRESHOLD = 0.10
SWAP_USED_THRESHOLD = 0.50
SWAP_IN_THRESHOLD_BYTES = 100 * 1024 * 1024
LOAD_THRESHOLD = 2.0
SWAP_INTERVAL_MAX = timedelta(minutes=10)


def read_host_sample(session: Session) -> HostSample:
    mem = {
        line.split(":")[0]: int(line.split()[1]) * 1024
        for line in (PROC_ROOT / "meminfo").read_text().splitlines()
        if line.split(":")[0] in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    }
    vm = {
        line.split()[0]: int(line.split()[1])
        for line in (PROC_ROOT / "vmstat").read_text().splitlines()
    }
    disk = os.statvfs(STATVFS_PATH)
    redis_bytes: int | None = None
    try:
        with Redis.from_url(get_settings().redis_url) as client:
            redis_bytes = int(client.info("memory")["used_memory"])
    except RedisError:
        pass
    return HostSample(
        sampled_at=datetime.now(ET),
        load_15m=float((PROC_ROOT / "loadavg").read_text().split()[2]),
        mem_total_bytes=mem["MemTotal"],
        mem_available_bytes=mem["MemAvailable"],
        swap_total_bytes=mem["SwapTotal"],
        swap_free_bytes=mem["SwapFree"],
        pswpin=vm["pswpin"],
        pswpout=vm["pswpout"],
        oom_kill=vm["oom_kill"],
        swappiness=int((PROC_ROOT / "sys/vm/swappiness").read_text()),
        disk_used_bytes=(disk.f_blocks - disk.f_bfree) * disk.f_frsize,
        disk_avail_bytes=disk.f_bavail * disk.f_frsize,
        pg_bytes=session.execute(text("SELECT pg_database_size(current_database())")).scalar_one(),
        redis_used_memory_bytes=redis_bytes,
    )


def counter_delta(current: int, previous: int | None) -> int | None:
    if previous is None or current < previous:
        return None
    return current - previous


def evaluate_alerts(current: HostSample, previous: HostSample | None) -> None:
    disk = current.disk_used_bytes / (current.disk_used_bytes + current.disk_avail_bytes)
    memory = current.mem_available_bytes / current.mem_total_bytes
    swap = (current.swap_total_bytes - current.swap_free_bytes) / current.swap_total_bytes
    incoming = counter_delta(current.pswpin, previous.pswpin) if previous else None
    oom = counter_delta(current.oom_kill, previous.oom_kill) if previous else None
    conditions = [
        ("disk", disk >= DISK_THRESHOLD, disk, DISK_THRESHOLD),
        ("memory", memory < MEMORY_THRESHOLD, memory, MEMORY_THRESHOLD),
        ("swap_used", swap >= SWAP_USED_THRESHOLD, swap, SWAP_USED_THRESHOLD),
        (
            "swap_in",
            previous is not None
            and current.sampled_at - previous.sampled_at <= SWAP_INTERVAL_MAX
            and incoming is not None
            and incoming * PAGE_BYTES >= SWAP_IN_THRESHOLD_BYTES,
            incoming * PAGE_BYTES if incoming is not None else None,
            SWAP_IN_THRESHOLD_BYTES,
        ),
        ("oom", oom is not None and oom > 0, oom, 0),
        ("load", current.load_15m >= LOAD_THRESHOLD, current.load_15m, LOAD_THRESHOLD),
    ]
    for metric, crossed, value, threshold in conditions:
        if not crossed:
            continue
        key = f"host-alert:{metric}:{current.sampled_at.astimezone(ET).date()}"
        if already_alerted(key):
            continue
        if send_ops_alert(
            subject=f"Portfonia host alert: {metric}",
            body=f"Metric: {metric}\nCurrent value: {value}\nThreshold: {threshold}",
            severity="ALERT",
        ):
            mark_alerted(key, ttl_seconds=2 * 86400)


def sample_and_alert(session: Session) -> HostSample:
    current = read_host_sample(session)
    session.add(current)
    session.commit()
    previous = session.scalar(
        select(HostSample)
        .where(HostSample.sampled_at < current.sampled_at)
        .order_by(HostSample.sampled_at.desc())
        .limit(1)
    )
    evaluate_alerts(current, previous)
    return current
