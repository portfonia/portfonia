"""Reserve every paid call under the process lock, before sending it."""

import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.alert_dedup import already_alerted, mark_alerted
from app.core.config import Settings
from app.core.timezones import ET
from app.models.intel import IntelSlotRun
from app.models.paid_intel import PaidApiUsage
from app.services.email_sender import send_ops_alert

_LOCK = threading.RLock()


@dataclass(frozen=True)
class Reservation:
    provider: str
    amount: Decimal


class PaidUsage:
    def __init__(
        self, session: Session, run: IntelSlotRun, settings: Settings, weekend: bool, now: datetime
    ) -> None:
        self.session = session
        self.run_id = run.id
        self.now = now.astimezone(ET)
        self.month = self.now.strftime("%Y-%m")
        self.settings = settings
        self.caps = {
            "tavily": Decimal(
                settings.TAVILY_WEEKEND_RUN_CREDIT_CAP
                if weekend
                else settings.TAVILY_RUN_CREDIT_CAP
            ),
            "parallel": settings.PARALLEL_WEEKEND_RUN_USD_CAP
            if weekend
            else settings.PARALLEL_RUN_USD_CAP,
        }
        self.limits = {
            "tavily": Decimal(settings.TAVILY_MONTHLY_CREDIT_LIMIT),
            "parallel": settings.PARALLEL_MONTHLY_USD_LIMIT,
        }
        self.configured = {
            "tavily": bool(settings.TAVILY_API_KEY),
            "parallel": bool(settings.PARALLEL_API_KEY),
        }
        self.run_used = {}
        self.month_used = {}
        self.reserved = dict.fromkeys(self.caps, Decimal(0))
        self.skipped = dict.fromkeys(self.caps, 0)
        self.disabled: set[str] = set()
        floor = self.now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for provider in self.caps:
            column = PaidApiUsage.units if provider == "tavily" else PaidApiUsage.cost_usd
            self.month_used[provider] = Decimal(
                session.scalar(
                    select(func.coalesce(func.sum(column), 0)).where(
                        PaidApiUsage.provider == provider,
                        PaidApiUsage.created_at >= floor,
                        PaidApiUsage.created_at <= self.now,
                    )
                )
                or 0
            )
            self.run_used[provider] = Decimal(
                session.scalar(
                    select(func.coalesce(func.sum(column), 0)).where(
                        PaidApiUsage.provider == provider, PaidApiUsage.slot_run_id == run.id
                    )
                )
                or 0
            )

    def _alert(self, provider: str, kind: str, category: str = "") -> None:
        key = f"paid-api-{kind}-{provider}-{category + '-' if category else ''}{self.now.date() if kind == 'error' else self.month}"
        if already_alerted(key):
            return
        severity: Literal["WARNING", "ALERT"] = "WARNING" if kind == "warn" else "ALERT"
        body = f"{provider} usage at {self.month_used[provider]} of {self.limits[provider]} this month; check the API key / switch to the backup key. {category}"
        if send_ops_alert(
            f"[Portfonia] {provider} paid API {kind}", body, idempotency_key=key, severity=severity
        ):
            mark_alerted(key, (2 if kind == "error" else 40) * 86400)

    def available(self) -> set[str]:
        with _LOCK:
            for provider in self.caps:
                if self.configured[provider] and self.month_used[provider] >= self.limits[provider]:
                    self._alert(provider, "limit")
            return {
                p
                for p in self.caps
                if self.configured[p]
                and p not in self.disabled
                and self.month_used[p] + self.reserved[p] < self.limits[p]
                and self.run_used[p] + self.reserved[p] < self.caps[p]
            }

    def reserve(self, provider: str, estimate: Decimal) -> Reservation | None:
        with _LOCK:
            if (
                self.month_used[provider] + self.reserved[provider] + estimate
                > self.limits[provider]
            ):
                self._alert(provider, "limit")
            elif (
                self.configured[provider]
                and provider not in self.disabled
                and self.run_used[provider] + self.reserved[provider] + estimate
                <= self.caps[provider]
            ):
                self.reserved[provider] += estimate
                return Reservation(provider, estimate)
            self.skipped[provider] += 1
            return None

    def skip(self, provider: str) -> None:
        with _LOCK:
            self.skipped[provider] += 1

    def extract_size(self, provider: str, count: int) -> int:
        with _LOCK:
            left = min(
                self.caps[provider] - self.run_used[provider] - self.reserved[provider],
                self.limits[provider] - self.month_used[provider] - self.reserved[provider],
            )
            maximum = int(left) * 5 if provider == "tavily" else int(left / Decimal(".001"))
            return max(0, min(count, maximum))

    def release(self, reservation: Reservation) -> None:
        with _LOCK:
            self.reserved[reservation.provider] -= reservation.amount

    def complete(
        self,
        reservation: Reservation,
        operation: str,
        units: Decimal | int,
        cost: Decimal,
        status: int | None,
        *,
        session: Session | None = None,
        commit: bool = False,
    ) -> None:
        with _LOCK:
            provider = reservation.provider
            if provider == "tavily":
                units = max(Decimal(units), reservation.amount)
                cost = Decimal(units) * Decimal(".008")
            else:
                cost = max(cost, reservation.amount)
            target = session or self.session
            target.add(
                PaidApiUsage(
                    provider=provider,
                    slot_run_id=self.run_id,
                    operation=operation,
                    units=Decimal(units),
                    cost_usd=cost,
                    http_status=status,
                    created_at=self.now,
                )
            )
            target.flush()
            if commit:
                target.commit()
            actual = Decimal(units) if provider == "tavily" else cost
            self.reserved[provider] -= reservation.amount
            self.run_used[provider] += actual
            self.month_used[provider] += actual
            if (
                self.month_used[provider]
                >= Decimal(str(self.settings.PAID_API_WARN_RATIO)) * self.limits[provider]
            ):
                self._alert(provider, "warn")

    def disable(self, provider: str, category: str) -> None:
        with _LOCK:
            self.disabled.add(provider)
            self._alert(provider, "error", category)
