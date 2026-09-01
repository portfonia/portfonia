"""ORM/dataclass -> JSON-serialisable dict conversion for report_inputs.

Split out of report_generator.py (#37). Pure data-shaping: no LLM calls, no
prompt text, no rendering.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.services.macro_detector import MacroSignals
from app.services.news_fetcher import NewsItem
from app.services.portfolio_calculator import PortfolioSnapshot
from app.services.price_anomaly_detector import PriceAnomaly
from app.services.technical_position import TechnicalPosition


def _serialize_news(items: list[NewsItem]) -> list[dict[str, Any]]:
    return [
        {
            "title": it.title,
            "source": it.source,
            "url": it.url,
            "published_at": it.published_at.isoformat(),
            "summary": it.summary,
        }
        for it in items
    ]


def _serialize_macro(signals: MacroSignals) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    for h in signals.hits:
        hits.append(
            {
                "theme": h.theme,
                "keywords_found": h.keywords_found,
                "article_count": len(h.articles),
                "top_articles": [{"title": a.title, "source": a.source} for a in h.articles[:3]],
            }
        )
    return {
        "has_any_hit": signals.has_any_hit,
        "total_matched_articles": signals.total_matched_articles,
        "hits": hits,
    }


def _f(x: Decimal | None) -> float | None:
    return float(x) if x is not None else None


def _serialize_anomalies(anomalies: list[PriceAnomaly]) -> list[dict[str, Any]]:
    return [
        {
            "name": a.name,
            "identifier": a.identifier,
            "asset_type": a.asset_type,
            "pct_change": float(a.pct_change),
            "threshold": float(a.threshold),
            "current_price": float(a.current_price),
            "prev_price": float(a.prev_price),
            "trigger": a.trigger,
            "market": a.market,
            "baseline_date": a.baseline_date.isoformat() if a.baseline_date else None,
            "latest_date": a.latest_date.isoformat() if a.latest_date else None,
            "window_net_pct": _f(a.window_net_pct),
            "max_day_pct": _f(a.max_day_pct),
            "max_day_date": a.max_day_date.isoformat() if a.max_day_date else None,
            "prev_close": _f(a.prev_close),
            "day_open": _f(a.day_open),
            "day_high": _f(a.day_high),
            "day_low": _f(a.day_low),
            "day_close": _f(a.day_close),
            "after_hours": _f(a.after_hours),
            "theme": a.theme,
            "theme_label_zh": a.theme_label_zh,
            "theme_label_en": a.theme_label_en,
            "constituents": [
                {
                    "name": c.name,
                    "identifier": c.identifier,
                    "pct_change": float(c.pct_change),
                    "current_value": float(c.current_value),
                }
                for c in (a.constituents or [])
            ],
        }
        for a in anomalies
    ]


def _serialize_technical(positions: list[TechnicalPosition]) -> list[dict[str, Any]]:
    return [
        {
            "ticker": p.ticker,
            "name": p.name,
            "last_close": p.last_close,
            "bars": p.bars,
            "pct_vs_sma50": p.pct_vs_sma50,
            "pct_vs_sma200": p.pct_vs_sma200,
            "range_52w_low": p.range_52w_low,
            "range_52w_high": p.range_52w_high,
            "pct_in_52w_range": p.pct_in_52w_range,
            "vol_20d_annualized": p.vol_20d_annualized,
        }
        for p in positions
    ]


def _serialize_portfolio(snap: PortfolioSnapshot) -> dict[str, Any]:
    holdings_list = [
        {
            "name": hv.name,
            "ticker": hv.ticker,
            "fund_code": hv.fund_code,
            "currency": hv.currency,
            "asset_type": hv.asset_type,
            "asset_class": hv.asset_class,
            "sector": hv.sector,
            "market": hv.market,
            "broker": hv.broker,
            "market_value": float(hv.market_value) if hv.market_value is not None else None,
            "market_value_base": (
                float(hv.market_value_base) if hv.market_value_base is not None else None
            ),
            "price_as_of": hv.price_as_of.isoformat() if hv.price_as_of else None,
            "position": hv.position if hv.position is not None else 1_000_000,
        }
        for hv in snap.holdings
    ]
    return {
        "base_currency": snap.base_currency,
        "fx_date": snap.fx_date.isoformat(),
        "total_base": float(snap.total_base),
        "by_market": {k: float(v) for k, v in snap.by_market.items()},
        "by_currency": {k: float(v) for k, v in snap.by_currency.items()},
        "by_asset_type": {k: float(v) for k, v in snap.by_asset_type.items()},
        "by_sector": {k: float(v) for k, v in snap.by_sector.items()},
        "by_asset_class": {k: float(v) for k, v in snap.by_asset_class.items()},
        "concentration": {
            "top_holding_name": snap.concentration.top_holding_name,
            "top_holding_ratio": float(snap.concentration.top_holding_ratio)
            if snap.concentration.top_holding_ratio is not None
            else None,
            "top_holding_asset_class": snap.concentration.top_holding_asset_class,
            "top3_ratio": float(snap.concentration.top3_ratio)
            if snap.concentration.top3_ratio is not None
            else None,
            "top_asset_class_name": snap.concentration.top_asset_class_name,
            "top_asset_class_ratio": float(snap.concentration.top_asset_class_ratio)
            if snap.concentration.top_asset_class_ratio is not None
            else None,
            "single_holding_watch": snap.concentration.single_holding_watch,
            "single_holding_high": snap.concentration.single_holding_high,
            "top3_watch": snap.concentration.top3_watch,
            "asset_class_watch": snap.concentration.asset_class_watch,
            "asset_class_high": snap.concentration.asset_class_high,
        },
        "stale_tickers": snap.stale_tickers,
        "stale_priced_tickers": snap.stale_priced_tickers,
        "holdings": holdings_list,
    }
