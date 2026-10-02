"""Plain-text collection digest, assembled only from URL-free run evidence."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.intel import IntelCollectionRun, IntelSlotRun


def obj(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def number(value: object) -> float:
    return float(value) if isinstance(value, (float, int)) else 0


def build_slot_digest(
    session: Session, slot: IntelSlotRun
) -> tuple[str, str, Literal["INFO", "WARNING"]]:
    previous = session.scalar(
        select(IntelSlotRun.started_at)
        .where(IntelSlotRun.started_at < slot.started_at)
        .order_by(IntelSlotRun.started_at.desc())
        .limit(1)
    )
    cutoff = previous or slot.started_at - timedelta(hours=24)
    runs = list(
        session.scalars(
            select(IntelCollectionRun)
            .where(
                IntelCollectionRun.started_at >= cutoff,
                IntelCollectionRun.started_at <= slot.finished_at
                if slot.finished_at
                else IntelCollectionRun.started_at <= slot.started_at,
            )
            .order_by(IntelCollectionRun.started_at)
        )
    )
    runs.sort(
        key=lambda run: (
            not (run.kind == "instrument" and run.slot_run_id == slot.id),
            run.started_at,
        )
    )
    severity: Literal["INFO", "WARNING"] = "WARNING" if slot.status == "failed" else "INFO"

    def fmt(dt: datetime) -> str:
        return dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")

    lines = [
        f"Slot: {slot.slot} {slot.run_date}",
        f"Start: {fmt(slot.started_at)}",
        f"Finish: {fmt(slot.finished_at) if slot.finished_at else 'running'}",
        f"Status: {slot.status}",
        "",
        "Collection and RSS nodes:",
    ]
    totals: dict[str, dict[str, float]] = {}
    feeds: dict[str, dict[str, float]] = {}
    cleaning: dict[str, float] = {}
    samples: dict[str, list[str]] = {}
    classifier: dict[str, float] = {}
    errors = []
    reserved = {"markets", "feeds", "cleaning", "cleaning_samples", "classifier", "unreached"}
    for run in runs:
        duration = (run.finished_at - run.started_at).total_seconds() if run.finished_at else 0
        if run.kind == "rss":
            lines.append(
                f"RSS node {run.node or 'capture-news'}: {fmt(run.started_at)} {run.status}; {duration:.1f}s"
            )
        elif run.slot_run_id == slot.id:
            lines.append(f"Instrument collection: {run.status}; {duration:.1f}s")
            unreached = obj(run.stats.get("unreached"))
            for market, raw in obj(run.stats.get("markets")).items():
                values = obj(raw)
                processed = number(values.get("processed"))
                total = number(values.get("total"))
                ids = unreached.get(market, [])
                text = ",".join(str(i) for i in ids) if isinstance(ids, list) else ""
                lines.append(
                    f"{market}: {processed:g}/{total:g} ({processed / total * 100 if total else 100:.1f}%); unreached: {text or 'none'}"
                )
        if run.status == "failed":
            severity = "WARNING"
        for source, raw in run.stats.items():
            if source in reserved:
                continue
            values = obj(raw)
            calls = number(values.get("calls"))
            failures = number(values.get("errors"))
            if calls and failures >= calls:
                severity = "WARNING"
            target = totals.setdefault(source, {})
            for key, value in values.items():
                target[key] = target.get(key, 0) + number(value)
        for feed, raw in obj(run.stats.get("feeds")).items():
            target = feeds.setdefault(feed, {})
            for key, value in obj(raw).items():
                target[key] = target.get(key, 0) + number(value)
        for key, value in obj(run.stats.get("cleaning")).items():
            cleaning[key] = cleaning.get(key, 0) + number(value)
        for key, value in obj(run.stats.get("classifier")).items():
            classifier[key] = classifier.get(key, 0) + number(value)
        for key, value in obj(run.stats.get("cleaning_samples")).items():
            if isinstance(value, list):
                samples[key] = (samples.get(key, []) + [str(x) for x in value])[:3]
        errors.extend(run.errors)
    lines += ["", "Per-source totals:"]
    for source, source_totals in totals.items():
        lines.append(
            source + ": " + ", ".join(f"{key}={value:g}" for key, value in source_totals.items())
        )
    lines += ["", "RSS feeds:"]
    for feed, feed_totals in feeds.items():
        lines.append(
            f"{feed}: items={feed_totals.get('items', 0):g}, errors={feed_totals.get('errors', 0):g}"
        )
    lines += ["", "Errors:", *list(dict.fromkeys(errors))[:20], "", "Cleaning:"]
    for reason, count in cleaning.items():
        lines.append(
            f"{reason}: {count:g}"
            + ("; samples: " + "; ".join(samples[reason]) if samples.get(reason) else "")
        )
    lines.append(
        "Classifier: " + ", ".join(f"{key}={value:g}" for key, value in classifier.items())
    )
    return (
        f"[Portfonia] Intel slot {slot.slot} {slot.run_date} - {slot.status}",
        "\n".join(lines),
        severity,
    )
