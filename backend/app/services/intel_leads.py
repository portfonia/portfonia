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
from app.models.paid_intel import IntelArticle
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_profiles import match_instruments
from app.services.intel_deepen_config import DeepenConfig
from app.services.intel_http import quiet_transport
from app.services.intel_selection import WorkUnit


@dataclass(frozen=True)
class Lead:
    url: str
    title: str
    published_at: datetime | None = None
    news_id: uuid.UUID | None = None


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
    previous: datetime,
) -> list[Lead]:
    candidates = []
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
                    NewsInstrument.created_at > previous,
                )
            )
            is None
        ):
            continue
        candidates.append(item)
    candidates.sort(
        key=lambda item: (
            (
                "finance.yahoo.com" in (urlsplit(item.url).hostname or ""),
                -item.published_at.timestamp(),
            )
            if unit.identifier
            else (False, -item.published_at.timestamp())
        )
    )
    limit = (
        cfg.caps.leads_per_mover
        if unit.kind == "mover"
        else cfg.caps.leads_per_quiet
        if unit.kind == "quiet"
        else cfg.caps.macro_links_per_theme
    )
    out = []
    domains = set()
    for item in candidates:
        url = resolve_redirect(item.url) if item.url_kind == "finnhub_redirect" else item.url
        if not url or excluded(url, cfg) or accepted_recently(session, url, cfg, now):
            continue
        host = urlsplit(url).hostname
        if host in domains:
            continue
        domains.add(host)
        out.append(Lead(url, item.title, item.published_at, item.news_id))
        if len(out) >= limit:
            break
    return out
