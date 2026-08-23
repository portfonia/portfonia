"""Celery task for /admin/* ops alerts (issue #129 Ring 1 stage B, checkpoint B2).

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


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.admin_tasks.send_admin_alert_task",
    # No caller ever waits on this task's result — ignoring it means .delay()
    # never touches the Celery result backend, only the broker. This matters
    # under a real outage: reproduced empirically (PR #177 review round 4)
    # that with the result backend enabled, .delay() against an unreachable
    # Redis retried for ~19s before raising; with ignore_result=True, the
    # same failure surfaces in under a second from the broker connection
    # alone (the router still isolates that failure in its own try/except —
    # see app/routers/admin.py).
    ignore_result=True,
)
def send_admin_alert_task(subject: str, body: str) -> None:
    send_ops_alert(subject, body)
