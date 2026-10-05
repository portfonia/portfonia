"""Caller-scoped, cache-only report and intelligence projections (#652)."""

from datetime import date, datetime, time, timedelta
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import timezones
from app.models.holding import Holding
from app.models.intel import IntelSlotRun, NewsInstrument
from app.models.news import News
from app.models.paid_intel import IntelArticle, IntelArticleLink
from app.models.report import Report
from app.schemas.agent import (
    AgentArticleOut,
    AgentHeadlineOut,
    AgentHoldingIntelOut,
    AgentIntelOut,
    AgentMacroOut,
    AgentMacroThemeOut,
    AgentReportOut,
    AgentReportsOut,
)
from app.services.instrument_symbols import InstrumentKey, intelligence_identifier
from app.services.intel_body import without_urls
from app.services.intel_records import headline_from_row

_REPORT_TYPES: dict[str, Literal["daily", "mwf", "weekly", "manual", "other"]] = {
    "daily_close": "daily",
    "after_close": "mwf",
    "weekend_snapshot": "weekly",
    "manual": "manual",
}
_AVAILABLE = ("success", "skipped")


def pull_reports(session: Session, user_id: UUID, start: date, end: date) -> AgentReportsOut:
    if start > end or end > timezones.today_et():
        raise HTTPException(422, "start must not be after end; end must not be after today ET")
    query = select(Report).where(
        Report.user_id == user_id,
        Report.report_date >= start,
        Report.report_date <= end,
        Report.status.in_(_AVAILABLE),
    )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.scalars(
        query.order_by(Report.report_date.desc(), Report.created_at.desc()).limit(5)
    )
    items = [
        AgentReportOut(
            id=r.id,
            report_date=r.report_date,
            type=_REPORT_TYPES.get(r.session_node, "other"),
            status="skipped" if r.status == "skipped" else "success",
            period_start=r.period_start,
            period_end=r.period_end,
            generated_at=r.generated_at,
            report_md=r.report_md,
        )
        for r in rows
    ]
    return AgentReportsOut(
        start=start, end=end, total_in_range=total, truncated=total > 5, items=items
    )


def pull_intel(session: Session, user_id: UUID, day: date) -> AgentIntelOut:
    today = timezones.today_et()
    if not today - timedelta(days=6) <= day <= today:
        raise HTTPException(422, "date must be within the last 7 days including today ET")
    identifiers = list(
        dict.fromkeys(
            intelligence_identifier(InstrumentKey("ticker", h.ticker))
            for h in session.scalars(
                select(Holding).where(Holding.user_id == user_id).order_by(Holding.position)
            )
            if h.ticker
        )
    )
    start = datetime.combine(day, time.min, tzinfo=timezones.ET)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezones.ET)
    seen_ids: set[UUID] = set()
    seen_keys: set[str] = set()

    def articles(
        *, identifier: str | None = None, theme: str | None = None
    ) -> list[AgentArticleOut]:
        owner = (
            IntelArticleLink.identifier == identifier
            if identifier is not None
            else IntelArticleLink.theme == theme
        )
        rows = session.scalars(
            select(IntelArticle)
            .join(IntelArticleLink, IntelArticleLink.article_id == IntelArticle.id)
            .join(IntelSlotRun, IntelSlotRun.id == IntelArticle.slot_run_id)
            .where(owner, IntelArticle.status == "accepted", IntelSlotRun.run_date == day)
            .order_by(IntelArticle.fetched_at.desc())
        )
        result = []
        for row in rows:
            if row.id in seen_ids or row.url_key in seen_keys:
                continue
            record = row.record or {}
            title, body = record.get("title"), record.get("body")
            if not isinstance(title, str) or not isinstance(body, str):
                continue
            published_at = record.get("published_at")
            result.append(
                AgentArticleOut(
                    title=title,
                    body=body,
                    published_at=published_at if isinstance(published_at, str) else None,
                )
            )
            seen_ids.add(row.id)
            seen_keys.add(row.url_key)
        return result

    holdings = []
    for identifier in identifiers:
        headlines = []
        for row in session.scalars(
            select(News)
            .join(NewsInstrument, NewsInstrument.news_id == News.id)
            .where(
                NewsInstrument.identifier == identifier,
                News.published_at >= start,
                News.published_at < end,
            )
            .order_by(News.published_at.desc())
        ):
            headline = headline_from_row(row)
            headlines.append(
                AgentHeadlineOut(
                    title=without_urls(headline.title),
                    published_at=headline.published_at,
                    summary=without_urls(headline.summary)
                    if headline.summary is not None
                    else None,
                )
            )
        bodies = articles(identifier=identifier)
        if headlines or bodies:
            holdings.append(
                AgentHoldingIntelOut(identifier=identifier, headlines=headlines, articles=bodies)
            )

    report = session.scalar(
        select(Report)
        .where(Report.user_id == user_id, Report.report_date <= day, Report.status.in_(_AVAILABLE))
        .order_by(Report.report_date.desc(), Report.created_at.desc())
        .limit(1)
    )
    macro = None
    if report is not None:
        signals = (report.report_inputs or {}).get("macro_signals", {})
        hits = signals.get("hits", []) if isinstance(signals, dict) else []
        themes = (
            list(
                dict.fromkeys(
                    hit["theme"]
                    for hit in hits
                    if isinstance(hit, dict) and isinstance(hit.get("theme"), str) and hit["theme"]
                )
            )
            if isinstance(hits, list)
            else []
        )
        macro = AgentMacroOut(
            source_report_id=report.id,
            source_report_date=report.report_date,
            themes=[
                AgentMacroThemeOut(theme=theme, articles=articles(theme=theme)) for theme in themes
            ],
        )
    return AgentIntelOut(date=day, holdings=holdings, macro=macro)
