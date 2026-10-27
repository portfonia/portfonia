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

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.services.ticker_leverage import DetectedProduct

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
def send_admin_alert_task(
    subject: str,
    body: str,
    severity: Literal["INFO", "WARNING", "ALERT"] = "ALERT",
    idempotency_key: str | None = None,
) -> None:
    if idempotency_key is None:
        send_ops_alert(subject, body, severity=severity)
    else:
        send_ops_alert(subject, body, severity=severity, idempotency_key=idempotency_key)


def alert_detected_products(products: list[DetectedProduct]) -> None:
    if not products:
        return
    body = "\n".join(
        [
            *[f"{p.ticker}: {p.long_name} | {p.category} | {p.fund_family}" for p in products],
            "",
            "Register with POST /admin/ticker-leverage {ticker, leverage_multiple, direction, asset_class, underlying_ticker}. Existing holdings are reclassified on registration.",
        ]
    )
    send_admin_alert_task.delay(
        subject=f"Leveraged/inverse product detected ({len(products)})",
        body=body,
        severity="WARNING",
    )


@celery_app.task(name="app.tasks.admin_tasks.check_leveraged_tickers_task", ignore_result=True)  # type: ignore[untyped-decorator]
def check_leveraged_tickers_task(tickers: list[str]) -> None:
    from app.services.ticker_leverage import detect_leveraged

    alert_detected_products(detect_leveraged(tickers))
