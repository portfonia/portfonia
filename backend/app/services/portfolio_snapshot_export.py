"""Shared complete daily snapshot export for web and agent readers."""

from datetime import date
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import today_et
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.schemas.portfolio import SnapshotDayOut, SnapshotExportOut, SnapshotHoldingOut
from app.services.portfolio_performance import _complete_batch_dates


def export_snapshot_range(
    session: Session, user_id: UUID, start: date, end: date
) -> SnapshotExportOut:
    if start > end:
        raise HTTPException(status_code=422, detail="start must not be after end")
    if end > today_et():
        raise HTTPException(status_code=422, detail="end must not be in the future")
    if (end - start).days > 29:
        raise HTTPException(status_code=422, detail="range must not exceed 30 days")

    dates = _complete_batch_dates(session, user_id, start, end)
    by_date: dict[date, list[PortfolioValueSnapshot]] = {day: [] for day in dates}
    if dates:
        rows = session.scalars(
            select(PortfolioValueSnapshot).where(
                PortfolioValueSnapshot.user_id == user_id,
                PortfolioValueSnapshot.snapshot_date.in_(dates),
            )
        )
        for row in rows:
            by_date[row.snapshot_date].append(row)

    days = []
    for day, day_rows in by_date.items():
        day_rows.sort(key=lambda row: (row.ticker or row.fund_code or "", str(row.holding_id)))
        days.append(
            SnapshotDayOut(
                date=day,
                base_currency=day_rows[0].base_currency if day_rows else None,
                holdings=[SnapshotHoldingOut.model_validate(row) for row in day_rows],
            )
        )
    return SnapshotExportOut(start=start, end=end, days=days)
