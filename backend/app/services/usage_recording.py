"""Independent, best-effort metadata accounting (#886)."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.operational_events import _get_sink_engine
from app.core.timezones import ET, today_et
from app.models.usage import ExternalCallDaily, HostSample, LlmUsage

logger = logging.getLogger(__name__)
PRICE_PER_CALL_USD = {"resend": Decimal("0.0009")}


@dataclass(frozen=True)
class LlmUsageContext:
    user_id: UUID | None
    purpose: str
    trigger_source: str


def record_llm_call(
    ctx: LlmUsageContext,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cost_usd: Decimal | float | None,
) -> None:
    try:
        with _get_sink_engine().begin() as conn:
            conn.execute(
                insert(LlmUsage).values(
                    user_id=ctx.user_id,
                    purpose=ctx.purpose,
                    trigger_source=ctx.trigger_source,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=cost_usd,
                )
            )
    except Exception as exc:
        logger.warning("LLM usage write failed (%s)", type(exc).__name__)


def count_external_call(provider: str) -> None:
    try:
        stmt = insert(ExternalCallDaily).values(day=today_et(), provider=provider, calls=1)
        with _get_sink_engine().begin() as conn:
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["day", "provider"], set_={"calls": ExternalCallDaily.calls + 1}
                )
            )
    except Exception as exc:
        logger.warning("External call count failed (%s)", type(exc).__name__)


def cleanup_usage(session: Session) -> dict[str, int]:
    cutoff = datetime.now(ET) - timedelta(days=90)
    totals: dict[str, int] = {}
    for model, column, keys, boundary in (
        (LlmUsage, LlmUsage.occurred_at, (LlmUsage.id,), cutoff),
        (HostSample, HostSample.sampled_at, (HostSample.id,), cutoff),
        (
            ExternalCallDaily,
            ExternalCallDaily.day,
            (ExternalCallDaily.day, ExternalCallDaily.provider),
            cutoff.date(),
        ),
    ):
        total = 0
        while True:
            rows = session.execute(select(*keys).where(column < boundary).limit(1000)).all()
            if not rows:
                break
            session.execute(delete(model).where(tuple_(*keys).in_([tuple(row) for row in rows])))
            session.commit()
            total += len(rows)
        totals[model.__tablename__ + "_deleted"] = total
    return totals
