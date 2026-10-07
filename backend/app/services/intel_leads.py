"""Current-task links; only their normalized hashes may cross storage."""

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.intel import NewsInstrument
from app.models.news import News
from app.models.paid_intel import IntelArticle
from app.services.headline_cleaning import MacroLabel
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_profiles import match_instruments
from app.services.intel_deepen_config import DeepenConfig
from app.services.intel_http import quiet_transport
from app.services.intel_records import headline_from_row
from app.services.intel_selection import WorkUnit

# Direct instrument leads prefer explanatory headlines (#681): unlabelled pool
# links and classifier failures sit between keep and passing mentions.
LABEL_RANK: dict[str | None, int] = {"keep": 0, None: 1, "mention": 2}


@dataclass(frozen=True)
class Lead:
    url: str
    title: str
    published_at: datetime | None = None
    news_id: uuid.UUID | None = None
    macro_label: MacroLabel | None = None


def url_key(url: str) -> str:
    p = urlsplit(url)
    normalized = urlunsplit(
        (
            p.scheme.lower(),
            p.netloc.lower(),
            p.path,
            urlencode(
                [
                    (k, v)
                    for k, v in parse_qsl(p.query, keep_blank_values=True)
                    if not k.lower().startswith("utm_")
                ]
            ),
            "",
        )
    )
    return hashlib.md5(normalized.encode()).hexdigest()[:16]


def excluded(url: str, cfg: DeepenConfig) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return urlsplit(url).scheme != "https" or any(
        host == d or host.endswith("." + d) for d in cfg.excluded_domains
    )


def resolve_redirect(url: str) -> str | None:
    for attempt in range(3):
        try:
            with (
                quiet_transport(),
                httpx.Client(timeout=12, follow_redirects=False) as client,
                client.stream("GET", url) as response,
            ):
                status = response.status_code
                location = response.headers.get("Location", "")
            if status == 302 and location.startswith("https://"):
                return cast(str, location)
            if status != 429 and status < 500:
                return None
        except httpx.TransportError:
            pass
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))
    return None


def accepted_recently(session: Session, url: str, cfg: DeepenConfig, now: datetime) -> bool:
    return (
        session.scalar(
            select(IntelArticle.id)
            .where(
                IntelArticle.url_key == url_key(url),
                IntelArticle.status == "accepted",
                IntelArticle.fetched_at >= now - timedelta(days=cfg.caps.accepted_url_skip_days),
            )
            .limit(1)
        )
        is not None
    )


def select_leads(
    session: Session,
    unit: WorkUnit,
    items: list[CollectedItem],
    aliases: list[str],
    cfg: DeepenConfig,
    now: datetime,
    *,
    macro_labels: dict[int, MacroLabel] | None = None,
    macro_selected_keys: set[str] | None = None,
    linked_existing: list[Lead] | None = None,
) -> list[Lead]:
    """Return extraction leads; return reused macro leads separately via linked_existing."""
    candidates = []
    ranked = (
        {id(item): macro_labels.get(i) for i, item in enumerate(items)}
        if macro_labels is not None
        else {}
    )
    for item in items:
        if item.url_kind not in ("direct", "finnhub_redirect") or item.kind == "filing":
            continue
        if unit.window_start and item.published_at.astimezone(ET).date() < unit.window_start:
            continue
        if unit.identifier and not match_instruments(item.title, {unit.identifier: aliases}):
            continue
        if unit.reason.startswith(("new_filing", "news_spike")) and (
            not item.news_id
            or session.scalar(
                select(NewsInstrument.id).where(
                    NewsInstrument.news_id == item.news_id,
                    NewsInstrument.identifier == unit.identifier,
                    NewsInstrument.created_at >= now,
                    NewsInstrument.relation.is_(None),
                )
            )
            is None
        ):
            continue
        label = ranked.get(id(item))
        if not unit.identifier and label and label["type"] != "development":
            continue
        candidates.append(item)

    def importance(item: CollectedItem) -> int:
        label = ranked.get(id(item))
        return -label["importance"] if label else 0

    candidates.sort(
        key=lambda item: (
            (
                LABEL_RANK.get(item.label, 1),
                "finance.yahoo.com" in (urlsplit(item.url).hostname or ""),
                -item.published_at.timestamp(),
            )
            if unit.identifier
            else (
                importance(item),
                False,
                -item.published_at.timestamp(),
            )
        )
    )
    limit = (
        cfg.caps.leads_per_mover
        if unit.kind == "mover"
        else cfg.caps.leads_per_quiet
        if unit.kind == "quiet"
        else cfg.caps.macro_links_per_theme
    )
    out: list[Lead] = []
    domains = set()
    events: set[str] = set()
    for item in candidates:
        label = ranked.get(id(item))
        url = resolve_redirect(item.url) if item.url_kind == "finnhub_redirect" else item.url
        if not url or excluded(url, cfg):
            continue
        if unit.kind == "macro" and url_key(url) in (macro_selected_keys or set()):
            if linked_existing is not None:
                linked_existing.append(
                    Lead(url, item.title, item.published_at, item.news_id, label)
                )
            continue
        if len(out) >= limit or (label and label["event"] in events):
            continue
        if accepted_recently(session, url, cfg, now):
            continue
        host = urlsplit(url).hostname
        if host in domains:
            continue
        domains.add(host)
        if label:
            events.add(label["event"])
        out.append(Lead(url, item.title, item.published_at, item.news_id, label))
        if len(out) >= limit and unit.kind != "macro":
            break
    return out


def select_headlines(
    session: Session,
    unit: WorkUnit,
    items: list[CollectedItem],
    aliases: list[str],
    cfg: DeepenConfig,
    now: datetime,
) -> list[CollectedItem]:
    candidates = []
    for item in items:
        if item.url_kind != "google_news" or item.kind == "filing":
            continue
        if unit.window_start and item.published_at.astimezone(ET).date() < unit.window_start:
            continue
        if unit.identifier and not match_instruments(item.title, {unit.identifier: aliases}):
            continue
        if unit.reason.startswith(("new_filing", "news_spike")) and (
            not item.news_id
            or session.scalar(
                select(NewsInstrument.id).where(
                    NewsInstrument.news_id == item.news_id,
                    NewsInstrument.identifier == unit.identifier,
                    NewsInstrument.created_at >= now,
                    NewsInstrument.relation.is_(None),
                )
            )
            is None
        ):
            continue
        candidates.append(item)
    candidates.sort(
        key=lambda item: (
            0 if item.label == "keep" else 1 if item.label == "mention" else 2,
            -item.published_at.timestamp(),
        )
    )
    return candidates[: cfg.caps.headlines_per_unit]


def stored_headlines(
    session: Session,
    unit: WorkUnit,
    aliases: list[str],
    cfg: DeepenConfig,
    now: datetime,
) -> list[CollectedItem]:
    """Earlier-batch keep headlines for a price-signal unit with no fresh material (#681).

    Only the stored title and publication time are used; the search that follows
    finds the article again, so no URL is read or reconstructed.
    """
    if not unit.identifier or not (unit.kind == "mover" or unit.reason.startswith("near_")):
        return []
    accepted = select(IntelArticle.news_id).where(
        IntelArticle.status == "accepted", IntelArticle.news_id.is_not(None)
    )
    query = (
        select(News)
        .join(NewsInstrument, NewsInstrument.news_id == News.id)
        .where(
            NewsInstrument.identifier == unit.identifier,
            NewsInstrument.relation.is_(None),
            NewsInstrument.created_at < now,
            News.kind == "article",
            News.intel_label == "keep",
            News.published_at <= now,
            News.id.not_in(accepted),
        )
        .order_by(News.published_at.desc())
    )
    if unit.window_start:
        query = query.where(
            News.published_at >= datetime.combine(unit.window_start, datetime.min.time(), tzinfo=ET)
        )
    result = []
    for row in session.scalars(query):
        headline = headline_from_row(row)
        if not match_instruments(headline.title, {unit.identifier: aliases}):
            continue
        result.append(
            CollectedItem(
                headline.title,
                row.published_at,
                "",
                url_kind="stored",
                identifier=unit.identifier,
                news_id=row.id,
                label="keep",
            )
        )
        if len(result) >= cfg.caps.headlines_per_unit:
            break
    return result
