"""The standard headline record boundary: URL-free writes and readers."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Boolean, delete, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.intel import IntelCollectionRun, IntelSlotRun, NewsInstrument
from app.models.news import News
from app.services.news_fetcher import NewsItem, _strip_html


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", _strip_html(value)).strip()


def build_article_record(
    title: str, published_at: datetime | None, fetched_at: datetime, body: str
) -> dict[str, object]:
    from app.services.intel_body import without_urls

    def stamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    return {
        "v": 1,
        "kind": "article_body",
        "title": without_urls(clean_text(title)),
        "published_at": stamp(published_at) if published_at else None,
        "fetched_at": stamp(fetched_at),
        "body": body,
    }


def build_headline_record(
    item: NewsItem,
    kind: str,
    label: str | None,
    *,
    collected_at: datetime | None = None,
    filing_form: str | None = None,
) -> dict[str, object]:
    return {
        "v": 1,
        "kind": kind,
        "title": clean_text(item.title),
        "summary": clean_text(item.summary)[:500] if item.summary else None,
        "published_at": item.published_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "collected_at": (collected_at or datetime.now(UTC))
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "label": label,
        "filing_form": filing_form,
    }


def store_headline(
    session: Session,
    item: NewsItem,
    origin: str,
    kind: str,
    label: str | None,
    *,
    filing_form: str | None = None,
) -> tuple[uuid.UUID, bool]:
    stmt = insert(News).values(
        url_hash=item.url_hash,
        origin=origin,
        kind=kind,
        intel_label=label,
        published_at=item.published_at,
        record=build_headline_record(item, kind, label, filing_form=filing_form),
    )
    if origin == "pool":
        stmt = stmt.on_conflict_do_update(
            constraint="uq_news_url_hash",
            set_={"origin": "pool"},
            where=News.origin == "instrument",
        )
    else:
        stmt = stmt.on_conflict_do_nothing(constraint="uq_news_url_hash")
    # xmax distinguishes a newly inserted tuple from the pool promotion UPDATE.
    row = session.execute(
        stmt.returning(News.id, literal_column("xmax = 0", Boolean))
    ).one_or_none()
    if row is not None:
        return row[0], row[1]
    nid = session.execute(select(News.id).where(News.url_hash == item.url_hash)).scalar_one()
    return nid, False


def link_instrument(session: Session, news_id: uuid.UUID, identifier: str) -> int:
    return len(
        session.execute(
            insert(NewsInstrument)
            .values(news_id=news_id, identifier=identifier)
            .on_conflict_do_nothing(constraint="uq_news_instruments_key")
            .returning(NewsInstrument.id)
        ).all()
    )


def headline_from_row(row: News) -> NewsItem:
    summary = row.record.get("summary")
    return NewsItem(
        row.url_hash,
        str(row.record["title"]),
        "",
        "",
        row.published_at,
        summary if isinstance(summary, str) else None,
    )


def sweep_intel(session: Session, now: datetime) -> dict[str, int]:
    from app.models.paid_intel import IntelArticle, PaidApiUsage

    counts = {}
    for model, column, days in [
        (IntelArticle, IntelArticle.fetched_at, 30),
        (PaidApiUsage, PaidApiUsage.created_at, 400),
        (News, News.published_at, 30),
        (IntelCollectionRun, IntelCollectionRun.started_at, 90),
        (IntelSlotRun, IntelSlotRun.started_at, 90),
    ]:
        # Detach retained collection records before deleting their old slot parent.
        if model is IntelSlotRun:
            from sqlalchemy import update

            session.execute(
                update(IntelCollectionRun)
                .where(
                    IntelCollectionRun.slot_run_id.in_(
                        select(IntelSlotRun.id).where(column < now - timedelta(days=days))
                    )
                )
                .values(slot_run_id=None)
            )
        result = session.execute(
            delete(model).where(column < now - timedelta(days=days)).returning(model.__table__.c.id)
        )
        counts[model.__tablename__ + "_deleted"] = len(result.all())
    return counts
