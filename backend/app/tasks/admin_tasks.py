"""Celery task for /admin/* ops alerts (issue #128 Ring 1 stage B, checkpoint B2).

`send_ops_alert` makes a blocking `httpx.Client(timeout=15.0)` call to Resend.
Every other call site in this codebase already runs inside a Celery task (a
separate worker process, decoupled from the FastAPI web server) —
`app/routers/admin.py` was the one place that called it directly from an
async request path, which could stall the single uvicorn worker's event loop
for up to 15s on every 5th unauthorized `/admin/*` hit (PR #177 review round
3). This task closes that gap by routing through the same queue every other
ops alert already uses, instead of inventing a process-local workaround
(e.g. a Starlette `BackgroundTask`) for a problem this codebase already has
a standard answer to.
"""

from __future__ import annotations

from app.services.email_sender import send_ops_alert
from app.tasks import celery_app


@celery_app.task(name="app.tasks.admin_tasks.send_admin_alert_task")  # type: ignore[untyped-decorator]
def send_admin_alert_task(subject: str, body: str) -> None:
    send_ops_alert(subject, body)
