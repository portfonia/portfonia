"""Minimal structured ops-event logging for Ring 0.

Writes JSON-structured lines to the standard logger under the prefix OPS_EVENT
so that admin actions (generate, regenerate, resend, task-triggered runs) can
be grep'd from .run/*.log without a dedicated table.

Ring 1 migration path: replace log_ops_event with an insert into an ops_log
table. The call sites are already in place.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def log_ops_event(event: str, **kwargs: Any) -> None:
    """Emit a structured ops log line.

    Usage:
        log_ops_event("report.generate.start", report_id=str(report.id), ...)

    Fields are emitted as JSON after the OPS_EVENT prefix so the line is both
    human-readable and machine-parseable (grep OPS_EVENT | jq .event).
    """
    payload = {"event": event, "ts": datetime.now(tz=UTC).isoformat(), **kwargs}
    logger.info("OPS_EVENT %s", json.dumps(payload, default=str))
