"""Public name cache and pure alias matching; never read Holding.name."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import yfinance as yf
from sqlalchemy.orm import Session

from app.models.intel import InstrumentProfile
from app.services._yfinance import _quiet_yfinance_logs
from app.services.holding_news import load_entity_aliases
from app.services.instrument_universe import UniverseEntry


def match_instruments(text: str, alias_map: Mapping[str, Sequence[str]]) -> set[str]:
    matches = set()
    for identifier, aliases in alias_map.items():
        for alias in aliases:
            if not alias:
                continue
            cjk = bool(re.search(r"[\u3400-\u9fff]", alias))
            pattern = re.escape(alias) if cjk else r"\b" + re.escape(alias) + r"\b"
            if re.search(pattern, text, 0 if cjk or len(alias) <= 4 else re.IGNORECASE):
                matches.add(identifier)
                break
    return matches


_SUFFIX = re.compile(
    r",?\s+(Corp(oration)?|Inc(orporated)?|Holdings?|Ltd|Limited|PLC|Co|AG|SE|SA|N\.?V|KK|Co\.,? Ltd)\.?$",
    re.IGNORECASE,
)
_TRAILING = " ,.;:-"


def clean_name(raw: str | None, ticker_stem: str) -> str | None:
    if not raw:
        return None
    name = " ".join(raw.split())
    for _ in range(3):
        stripped = _SUFFIX.sub("", name).rstrip(_TRAILING)
        if stripped == name:
            break
        name = stripped
    name = name.rstrip(_TRAILING)
    if not name:
        return None
    if re.fullmatch(r"[A-Z0-9]+", name) and name != ticker_stem.upper():
        return None
    return name


def resolve_profiles(
    session: Session, entries: list[UniverseEntry], *, now: datetime | None = None
) -> list[str]:
    from app.core.config import get_settings
    from app.services.instrument_news_sources import (
        eastmoney_rows,
        mapping,
        request,
        rows,
        text_value,
    )

    now = now or datetime.now(UTC)
    overrides = load_entity_aliases()
    errors = []
    for entry in entries:
        old = session.get(InstrumentProfile, entry.identifier)
        manual = [a.strip() for a in overrides.get(entry.identifier, []) if a.strip()]
        stem = entry.ticker.split(".")[0]
        if old and old.name_resolved_at and old.name_resolved_at >= now - timedelta(days=30):
            consistent = list(old.aliases or [])[: len(manual)] == manual and (
                old.name_source not in ("finnhub", "yfinance")
                or old.name_en is None
                or clean_name(old.name_en, stem) == old.name_en
            )
            if consistent:
                continue
        try:
            name_en = None
            name_zh = None
            source = None
            if manual:
                name_en = manual[0]
                source = "config"
            else:
                if entry.market == "US" and get_settings().FINNHUB_API_KEY:
                    try:
                        key = get_settings().FINNHUB_API_KEY
                        assert key is not None
                        data = mapping(
                            request(
                                "https://finnhub.io/api/v1/stock/profile2",
                                params={"symbol": entry.ticker, "token": key.get_secret_value()},
                            ).json()
                        )
                        name_en = text_value(data.get("name")) or None
                        source = "finnhub" if name_en else None
                    except Exception as exc:
                        errors.append(f"finnhub: {type(exc).__name__}")
                if not name_en:
                    with _quiet_yfinance_logs():
                        data = mapping(yf.Ticker(entry.ticker).info)
                    name_en = (
                        text_value(data.get("longName"))
                        or text_value(data.get("shortName"))
                        or None
                    )
                    source = "yfinance" if name_en else None
            if source in ("finnhub", "yfinance"):
                name_en = clean_name(name_en, stem)
                if name_en is None:
                    source = None
            if entry.market in ("A-Share", "HK"):
                code = entry.ticker.split(".")[0].zfill(5 if entry.market == "HK" else 6)
                announcements = eastmoney_rows(code, "H" if entry.market == "HK" else "A")
                if announcements:
                    for company in rows(announcements[0].get("codes", [])):
                        if str(company.get("stock_code")) == code:
                            name_zh = text_value(company.get("short_name")) or None
                            break
                if not source and name_zh:
                    source = "eastmoney"
            if not name_en and not name_zh:
                raise ValueError("name unavailable")
            p = old or InstrumentProfile(identifier=entry.identifier, market=entry.market)
            p.name_en = name_en
            p.name_zh = name_zh
            p.name_source = source
            p.aliases = list(dict.fromkeys([a for a in [*manual, name_en, name_zh, stem] if a]))
            p.name_resolved_at = now
            p.updated_at = now
            session.add(p)
        except Exception as exc:
            errors.append(f"profile: {type(exc).__name__}")
    session.flush()
    return errors
