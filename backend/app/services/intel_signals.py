"""Global selection signals from captured closes and fresh news links."""

from dataclasses import dataclass
from datetime import date, datetime
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.intel import IntelSlotRun, NewsInstrument
from app.models.news import News
from app.models.price_snapshot import PriceSnapshot
from app.services.instrument_universe import UniverseEntry
from app.services.intel_deepen_config import DeepenConfig


@dataclass
class Signal:
    identifier: str
    d1: float | None = None
    d3: float | None = None
    d5: float | None = None
    mover: bool = False
    near: bool = False
    strength: float = 0
    direction: str = "up"
    window_start: date | None = None
    reason: str = ""
    filings: int = 0
    fresh: int = 0
    news_spike: bool = False
    spike_ratio: float = 0

    @classmethod
    def from_closes(
        cls,
        identifier: str,
        closes: list[tuple[date, float]],
        cfg: DeepenConfig,
        run_date: date,
        slot: str,
        previous: datetime,
    ) -> "Signal":
        from datetime import timedelta

        signal = cls(identifier, window_start=previous.astimezone(ET).date())
        measures = {}
        for name, index in [("d1", 1), ("d3", 3), ("d5", 5)]:
            value = (
                closes[0][1] / closes[index][1] - 1
                if len(closes) > index and closes[index][1]
                else None
            )
            setattr(signal, name, value)
            if value is not None:
                measures[name] = value
        limits = {"d1": cfg.thresholds.single_day, "d3": cfg.thresholds.d3, "d5": cfg.thresholds.d5}
        triggered = {k: v for k, v in measures.items() if abs(v) >= limits[k]}
        if triggered:
            key = max(triggered, key=lambda k: abs(triggered[k]))
            signal.mover = True
            signal.strength = abs(triggered[key])
            signal.direction = "up" if triggered[key] >= 0 else "down"
            signal.reason = f"{key} {triggered[key]:+.1%}"
            signal.window_start = (
                closes[5][0]
                if "d5" in triggered
                else closes[3][0]
                if "d3" in triggered
                else run_date - timedelta(days=2 if slot == "pre_open" else 1)
            )
        else:
            near = {
                k: v
                for k, v in measures.items()
                if k in ("d3", "d5") and abs(v) >= getattr(cfg.thresholds, "near_" + k)
            }
            if near:
                key = max(near, key=lambda k: abs(near[k]) / getattr(cfg.thresholds, "near_" + k))
                signal.near = True
                signal.strength = abs(near[key]) / getattr(cfg.thresholds, "near_" + key)
                signal.reason = f"near_{key} {near[key]:+.1%}"
                signal.window_start = closes[5][0] if "d5" in near else closes[3][0]
        return signal


def slot_history(
    session: Session, slot: str, now: datetime, cfg: DeepenConfig, weekend: bool
) -> list[IntelSlotRun]:
    rows = list(
        session.scalars(
            select(IntelSlotRun)
            .where(IntelSlotRun.slot == slot, IntelSlotRun.started_at < now)
            .order_by(IntelSlotRun.started_at.desc())
        )
    )
    return [r for r in rows if not weekend or r.run_date.weekday() >= 5][
        : cfg.thresholds.news_spike_history_runs
    ]


def spike(fresh: int, history: list[int], cfg: DeepenConfig, weekend: bool) -> bool:
    minimum = (
        cfg.thresholds.weekend_min_history if weekend else cfg.thresholds.news_spike_min_history
    )
    return (
        fresh >= cfg.thresholds.news_spike_min_fresh
        and len(history) >= minimum
        and fresh >= cfg.thresholds.news_spike_ratio * median(history)
    )


def compute_signals(
    session: Session,
    universe: list[UniverseEntry],
    run_date: date,
    prev_slot_started_at: datetime,
    cfg: DeepenConfig,
    *,
    slot: str,
    now: datetime,
    weekend: bool = False,
) -> dict[str, Signal]:
    history = slot_history(session, slot, now, cfg, weekend)
    result = {}
    for entry in universe:
        closes = (
            []
            if weekend
            else [
                (r.trade_date, float(r.close))
                for r in session.scalars(
                    select(PriceSnapshot)
                    .where(
                        PriceSnapshot.ticker == entry.ticker,
                        PriceSnapshot.market == entry.market,
                        PriceSnapshot.session_node == "close",
                        PriceSnapshot.close.is_not(None),
                        PriceSnapshot.trade_date <= run_date,
                    )
                    .order_by(PriceSnapshot.trade_date.desc())
                    .limit(6)
                )
                if r.close is not None
            ]
        )
        signal = Signal.from_closes(
            entry.identifier, closes, cfg, run_date, slot, prev_slot_started_at
        )
        kinds = list(
            session.scalars(
                select(News.kind)
                .join(NewsInstrument, NewsInstrument.news_id == News.id)
                .where(
                    NewsInstrument.identifier == entry.identifier,
                    NewsInstrument.created_at >= now,
                )
            )
        )
        signal.fresh = len(kinds)
        signal.filings = kinds.count("filing")
        values = []
        for run in history:
            counts = run.details.get("fresh_counts", {})
            universe_ids = run.details.get("universe", [])
            if isinstance(counts, dict) and (
                entry.identifier in counts
                or (isinstance(universe_ids, list) and entry.identifier in universe_ids)
            ):
                values.append(int(counts.get(entry.identifier, 0)))
        signal.news_spike = spike(signal.fresh, values, cfg, weekend)
        signal.spike_ratio = (
            signal.fresh / median(values) if values and median(values) > 0 else float(signal.fresh)
        )
        result[entry.identifier] = signal
    return result
