"""Fixture-only host sampling and alert acceptance checks (#886)."""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any  # Mixed ORM fixture keyword values and mock responses.
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.core.timezones import ET


def sample_values(**changes: Any) -> dict[str, Any]:
    values = dict(
        sampled_at=datetime.now(ET),
        load_15m=0.1,
        mem_total_bytes=1000,
        mem_available_bytes=800,
        swap_total_bytes=1000,
        swap_free_bytes=1000,
        pswpin=0,
        pswpout=0,
        oom_kill=0,
        swappiness=10,
        disk_used_bytes=20,
        disk_avail_bytes=80,
        pg_bytes=100,
        redis_used_memory_bytes=200,
    )
    return values | changes


def test_13_sampler_parses_and_inserts_fixture_proc(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.usage import HostSample
    from app.services import host_metrics as hm
    from app.tasks.host_metrics_tasks import sample_host

    (tmp_path / "sys/vm").mkdir(parents=True)
    (tmp_path / "loadavg").write_text("0.1 0.2 1.5 1/10 10\n")
    (tmp_path / "meminfo").write_text(
        "MemTotal: 1000 kB\nMemAvailable: 700 kB\nSwapTotal: 500 kB\nSwapFree: 400 kB\n"
    )
    (tmp_path / "vmstat").write_text("pswpin 12\npswpout 34\noom_kill 2\n")
    (tmp_path / "sys/vm/swappiness").write_text("10\n")
    monkeypatch.setattr(hm, "PROC_ROOT", tmp_path)
    monkeypatch.setattr(hm, "STATVFS_PATH", str(tmp_path))
    stat = SimpleNamespace(f_blocks=100, f_bfree=80, f_bavail=75, f_frsize=4096)
    with (
        patch("os.statvfs", return_value=stat) as statvfs,
        patch.object(hm, "Redis") as redis,
        patch.object(hm, "evaluate_alerts"),
    ):
        redis.from_url.return_value.__enter__.return_value.info.return_value = {"used_memory": 456}
        result = sample_host.run()
    row = db_session.get(HostSample, result["id"])
    assert row is not None
    assert (
        row.load_15m,
        row.mem_total_bytes,
        row.mem_available_bytes,
        row.swap_total_bytes,
        row.swap_free_bytes,
        row.pswpin,
        row.pswpout,
        row.oom_kill,
        row.swappiness,
        row.disk_used_bytes,
        row.disk_avail_bytes,
        row.redis_used_memory_bytes,
    ) == (1.5, 1024000, 716800, 512000, 409600, 12, 34, 2, 10, 81920, 307200, 456)
    assert row.pg_bytes > 0
    statvfs.assert_called_once_with(str(tmp_path))


@pytest.mark.parametrize(
    "metric,changes,age",
    [
        ("disk", {"disk_used_bytes": 80, "disk_avail_bytes": 20}, 5),
        ("memory", {"mem_available_bytes": 99}, 5),
        ("swap_used", {"swap_free_bytes": 500}, 5),
        ("swap_in", {"pswpin": 25600}, 5),
        ("oom", {"oom_kill": 1}, 5),
        ("load", {"load_15m": 2.0}, 5),
        (None, {"pswpin": 25600}, 11),
        (None, {"pswpin": -1}, 5),
    ],
)
def test_14_alert_thresholds_and_swap_interval(
    metric: str | None, changes: dict[str, Any], age: int
) -> None:
    from app.models.usage import HostSample
    from app.services import host_metrics as hm

    current = HostSample(**sample_values(**changes))
    previous = HostSample(**sample_values(sampled_at=current.sampled_at - timedelta(minutes=age)))
    with (
        patch.object(hm, "already_alerted", return_value=False),
        patch.object(hm, "send_ops_alert", return_value=True) as send,
        patch.object(hm, "mark_alerted") as mark,
    ):
        hm.evaluate_alerts(current, previous)
    assert send.call_count == (1 if metric else 0)
    assert [c.args[0] for c in mark.call_args_list] == (
        [f"host-alert:{metric}:{current.sampled_at.astimezone(ET).date()}"] if metric else []
    )


def test_15_daily_dedup_and_failed_send() -> None:
    from app.models.usage import HostSample
    from app.services import host_metrics as hm

    current = HostSample(**sample_values(load_15m=2.0))
    seen = set()
    with (
        patch.object(hm, "already_alerted", side_effect=lambda key: key in seen),
        patch.object(
            hm, "mark_alerted", side_effect=lambda key, ttl_seconds: seen.add(key)
        ) as mark,
        patch.object(hm, "send_ops_alert", side_effect=[False, True]) as send,
    ):
        hm.evaluate_alerts(current, None)
        assert not mark.called
        hm.evaluate_alerts(current, None)
        hm.evaluate_alerts(current, None)
        assert send.call_count == 2 and mark.call_count == 1
        assert mark.call_args.kwargs["ttl_seconds"] == 2 * 86400


def test_18_beat_schedule_and_quiet_classification() -> None:
    from celery.schedules import crontab  # type: ignore[import-untyped]

    from app.tasks import API_QUIET_BEAT_ENTRIES, celery_app

    entry = celery_app.conf.beat_schedule["sample-host-every-5-minutes"]
    assert entry["task"] == "app.tasks.host_metrics_tasks.sample_host"
    assert entry["schedule"] == crontab(minute="*/5")
    assert API_QUIET_BEAT_ENTRIES["sample-host-every-5-minutes"] is False
