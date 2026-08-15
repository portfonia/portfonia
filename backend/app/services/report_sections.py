"""Code-built report sections — no LLM involved, deterministic from data.

Covers §1 Portfolio Snapshot, §2.5 Forward Calendar, §4.2 Price Anomalies
table, §4.4 Technical Position, the F3 fixed footer, and the data-window
statement. Split out of report_generator.py (#37).
"""

from __future__ import annotations

from datetime import date, datetime
from string import Template
from typing import Any

from app.core.timezones import ET
from app.services.i18n_glossary import load_i18n_glossary

# ---------------------------------------------------------------------------
# §1 Portfolio Snapshot
# ---------------------------------------------------------------------------


def _build_section1(portfolio: dict[str, Any]) -> str:
    """Build §1 Portfolio Snapshot entirely from data — no LLM."""
    base_ccy = portfolio.get("base_currency", "USD")
    fx_date = portfolio.get("fx_date", "N/A")
    total = portfolio.get("total_base", 0)

    lines: list[str] = [
        "## §1 Portfolio Snapshot",
        "",
        f"**Total value:** {base_ccy} {total:,.0f}  (FX date: {fx_date})",
        "",
        "| Holding | Currency | Value | % Portfolio | Custodian | Asset Class |",
        "|---------|----------|-------|-------------|-----------|-------------|",
    ]

    holdings = list(portfolio.get("holdings", []))
    # Group by custodian (holding institution), preserving the user's upload
    # order: institutions appear in the order they first show up in the file,
    # holdings within an institution keep their file order. Cash sits inside its
    # own institution. A holding with no declared institution falls into "Other".
    # Each group gets a subtotal so per-institution capital is legible.
    group_order: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for h in sorted(
        holdings,
        key=lambda x: x["position"] if x.get("position") is not None else 1_000_000,
    ):
        broker = h.get("broker") or "Other"
        if broker not in groups:
            groups[broker] = []
            group_order.append(broker)
        groups[broker].append(h)

    for broker in group_order:
        members = groups[broker]
        subtotal_base = sum(m.get("market_value_base", 0) for m in members)
        for h in members:
            mv = h.get("market_value", 0)
            mv_base = h.get("market_value_base", 0)
            ratio = mv_base / total if total > 0 else 0
            name_col = h["name"] + (f" ({h['ticker']})" if h.get("ticker") else "")
            lines.append(
                f"| {name_col} | {h.get('currency', '')} | {mv:,.0f} | {ratio:.1%} "
                f"| {h.get('broker', '') or '—'} | {h.get('asset_class', '—') or '—'} |"
            )
        sub_ratio = subtotal_base / total if total > 0 else 0
        lines.append(
            f"| **{broker} subtotal** | {base_ccy} | **{subtotal_base:,.0f}** "
            f"| **{sub_ratio:.1%}** | | |"
        )

    lines += [
        "",
        "**Distribution:**",
        "",
    ]
    for label, dist in [
        ("By market", portfolio.get("by_market", {})),
        ("By currency", portfolio.get("by_currency", {})),
        ("By asset class", portfolio.get("by_asset_class", {})),
    ]:
        if dist:
            parts = ", ".join(
                f"{k}: {v / total:.1%}"
                for k, v in sorted(dist.items(), key=lambda x: -x[1])
                if total > 0
            )
            lines.append(f"- **{label}:** {parts}")

    stale = portfolio.get("stale_tickers", [])
    if stale:
        lines.append(f"\n> [!] Stale/missing prices: {', '.join(stale)}")

    stale_priced = portfolio.get("stale_priced_tickers", [])
    if stale_priced:
        lines.append(
            f"\n> [!] Price data stale (>4 calendar days old): {', '.join(stale_priced)}"
            " — values included but may not reflect recent moves."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# §4.2 Price anomalies table
# ---------------------------------------------------------------------------


def _build_section42_table(anomalies: list[dict[str, Any]]) -> str:
    """Build the §4.2 price-anomaly table from data — no LLM (#3).

    The numeric session arc already lives in `report_inputs.price_anomalies`; a
    code-built table is deterministic, token-free, and cannot hallucinate a
    number. The LLM writes only the one-line driver per holding underneath
    (see the §4.2 prompt instruction). English headers are rendered to the
    output language by the translation pass, same as §1.

    For theme-aggregated entries (e.g. Gold = SGOL + 518660 + 518800), the
    headline row shows the weighted-average move; a "Constituents" block below
    the table lists each holding's individual contribution.
    """

    def pct(x: float | None) -> str:
        return f"{x * 100:+.2f}%" if x is not None else "—"

    def num(x: float | None) -> str:
        return f"{x:g}" if x is not None else "—"

    lines: list[str] = [
        "| Holding | Net % | Worst day (date) | Prev close | Open (gap %) "
        "| Intraday range | Close | After-hrs | Trigger |",
        "|---------|-------|------------------|------------|--------------"
        "|----------------|-------|-----------|---------|",
    ]
    theme_detail_lines: list[str] = []

    for a in anomalies:
        theme = a.get("theme")
        ident = a.get("identifier", "")

        if theme:
            # Theme entry: show label + theme key.  Constituents go in a note below.
            label_zh = a.get("theme_label_zh") or a.get("name", "")
            name_col = f"{label_zh} ({ident})" if ident != label_zh else label_zh
        else:
            name_col = f"{a.get('name', '')} ({ident})" if ident else a.get("name", "")

        wd = a.get("max_day_date")
        worst = pct(a.get("max_day_pct"))
        worst_col = f"{worst} ({wd})" if a.get("max_day_pct") is not None and wd else worst

        pc, op = a.get("prev_close"), a.get("day_open")
        open_col = f"{op:g} ({(op / pc - 1) * 100:+.1f}%)" if op is not None and pc else num(op)

        lo, hi = a.get("day_low"), a.get("day_high")
        range_col = f"{lo:g}-{hi:g}" if lo is not None and hi is not None else "—"

        cl, ah = a.get("day_close"), a.get("after_hours")
        ah_col = f"{ah:g} ({(ah / cl - 1) * 100:+.1f}%)" if ah is not None and cl else num(ah)

        lines.append(
            f"| {name_col} | {pct(a.get('window_net_pct'))} | {worst_col} "
            f"| {num(pc)} | {open_col} | {range_col} | {num(cl)} | {ah_col} "
            f"| {a.get('trigger', '')} |"
        )

        if theme:
            constituents: list[dict[str, Any]] = a.get("constituents") or []
            if len(constituents) >= 2:
                parts = ", ".join(
                    f"{c.get('identifier') or c.get('name')} {pct(c.get('pct_change'))}"
                    for c in constituents
                )
                label = a.get("theme_label_en") or ident
                theme_detail_lines.append(
                    f"- **{label}**: {parts} (weighted avg {pct(a.get('window_net_pct'))})"
                )

    result = "\n".join(lines)
    if theme_detail_lines:
        result += "\n\n**Constituent breakdown:**\n" + "\n".join(theme_detail_lines)
    return result


def _inject_section42_table(body: str, table: str) -> str:
    """Insert the code-built §4.2 anomaly table right after the §4.2 heading the
    LLM emits, above its one-line drivers (#3). Falls back to appending a §4.2
    block when the heading is absent so the table is never dropped silently."""
    out: list[str] = []
    injected = False
    for line in body.split("\n"):
        out.append(line)
        if not injected and line.lstrip().startswith("### 4.2"):
            out += ["", table, ""]
            injected = True
    if not injected:
        out += ["", "### 4.2 Price anomalies", "", table, ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# §4.4 Technical position
# ---------------------------------------------------------------------------


def _build_section44_technical(positions: list[dict[str, Any]]) -> str:
    """Build the §4.4 technical-position block from data — no LLM (#4).

    Pure description of where each holding's price sits: distance to its 50/200-day
    average, position inside the trailing 52-week range, recent realized volatility.
    No signals, no actions. Cells with insufficient captured history render as "—".
    If no holding has any computed metric yet (capture layer still warming up /
    backfill not run), a single explanatory line replaces the table.
    """

    def pct(x: float | None) -> str:
        return f"{x * 100:+.1f}%" if x is not None else "—"

    def rng(x: float | None) -> str:
        return f"{x * 100:.0f}%" if x is not None else "—"

    rows = [p for p in positions if p.get("ticker")]
    if not any(
        p.get("pct_vs_sma50") is not None
        or p.get("pct_vs_sma200") is not None
        or p.get("pct_in_52w_range") is not None
        or p.get("vol_20d_annualized") is not None
        for p in rows
    ):
        return (
            "### 4.4 Technical position\n\n"
            "Insufficient captured price history to compute technical position "
            "(run the one-year OHLCV backfill; metrics populate as the capture "
            "layer accumulates closes)."
        )

    lines = [
        "### 4.4 Technical position",
        "",
        "| Holding | vs 50-day avg | vs 200-day avg | 52-week range position "
        "| 20-day volatility (ann.) |",
        "|---------|---------------|----------------|-------------------------"
        "|--------------------------|",
    ]
    for p in rows:
        ident = p.get("ticker", "")
        name_col = f"{p.get('name', '')} ({ident})" if ident else p.get("name", "")
        lines.append(
            f"| {name_col} | {pct(p.get('pct_vs_sma50'))} | {pct(p.get('pct_vs_sma200'))} "
            f"| {rng(p.get('pct_in_52w_range'))} | {pct(p.get('vol_20d_annualized'))} |"
        )
    lines += [
        "",
        "> Range position: 0% = at the 52-week low, 100% = at the 52-week high. "
        "Figures describe where the price sits; they are not signals.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# §2.5 Forward calendar (code-built, injected into the LLM's already-generated
# body — not part of the prompt itself)
# ---------------------------------------------------------------------------

_RATE_SENSITIVE_SECTORS = {
    "Technology",
    "Consumer Discretionary",
    "Communication Services",
    "Real Estate",
}
_CONSUMER_SECTORS = {"Consumer Discretionary", "Consumer Staples"}
_GOLD_TICKERS = {"GLD", "IAU", "GLDM", "SGOL", "GLDX"}
# RSS-derived delay caveat triggers (#1): a funding lapse can suspend BLS/BEA
# releases. zh-Hans terms load from i18n_glossary.yml's release_delay_terms_zh
# — frozen into this module constant at import, so an admin edit to that YAML
# list needs a process restart to take effect (PR #91 review: unlike
# _build_footer/_stale_ticker_hint, which call load_i18n_glossary() fresh
# inside the function body and pick up an edit on the next call).
_RELEASE_DELAY_TERMS = (
    "government shutdown",
    "funding lapse",
    "funding gap",
    "appropriations lapse",
    "budget shutdown",
    *load_i18n_glossary().release_delay_terms_zh,
)


def _is_gold(h: dict[str, Any]) -> bool:
    ticker = (h.get("ticker") or "").upper()
    return "gold" in (h.get("name") or "").lower() or ticker in _GOLD_TICKERS


def _us_equity(h: dict[str, Any]) -> bool:
    return h.get("market") == "US" and h.get("asset_type") in ("stock", "etf")


def _forward_exposure(
    event: dict[str, Any], holdings: list[dict[str, Any]]
) -> tuple[list[str], str]:
    """Map a scheduled event to the holdings exposed to it + what to watch.

    Pure facts and observation framing — never a directional forecast.
    """
    if event.get("event_type") == "earnings":
        tk = event.get("ticker", "")
        exposed = [h["name"] for h in holdings if h.get("ticker") == tk]
        return (exposed or [tk]), "reported results vs expectations for this holding"

    low = event.get("name", "").lower()
    if "fomc" in low:
        return (
            [
                h["name"]
                for h in holdings
                if _us_equity(h) and (h.get("sector") in _RATE_SENSITIVE_SECTORS or _is_gold(h))
            ],
            "policy-rate decision and statement tone",
        )
    if any(k in low for k in ("cpi", "ppi", "pce", "personal income")):
        return (
            [
                h["name"]
                for h in holdings
                if _us_equity(h) and (h.get("sector") in _RATE_SENSITIVE_SECTORS or _is_gold(h))
            ],
            "inflation reading vs consensus; rate-path implications",
        )
    if "payroll" in low or "employment" in low:
        return (
            [h["name"] for h in holdings if _us_equity(h)],
            "labor-market strength; rate-path implications",
        )
    if "retail" in low:
        return (
            [h["name"] for h in holdings if h.get("sector") in _CONSUMER_SECTORS],
            "consumer-spending momentum",
        )
    if "sentiment" in low:
        return (
            [h["name"] for h in holdings if h.get("sector") in _CONSUMER_SECTORS],
            "household-sentiment trend",
        )
    if "gross domestic" in low or "gdp" in low:
        return ([h["name"] for h in holdings if _us_equity(h)], "growth pace vs consensus")
    return ([h["name"] for h in holdings if _us_equity(h)], "relevance to US-exposed holdings")


def _forward_delay_risk(news_items: list[dict[str, Any]]) -> bool:
    """True if window news mentions a funding lapse that could delay US releases."""
    for it in news_items:
        text = f"{it.get('title', '')} {it.get('summary') or ''}".lower()
        if any(term in text for term in _RELEASE_DELAY_TERMS):
            return True
    return False


def _build_forward_block(
    events: list[dict[str, Any]],
    holdings: list[dict[str, Any]],
    news_items: list[dict[str, Any]],
    report_date_str: str = "",
) -> str:
    """Build §2.5 Forward Calendar from data — no LLM (#1).

    Lists scheduled US macro releases + held-company earnings in the forward
    window and maps each to the holdings carrying exposure. Calendar facts only;
    the 'Watch' column points to an observation, never a predicted outcome. If
    window news flags a funding lapse, a delay caveat is appended. Events dated
    the report's own day are tagged '(today)' and also promoted to a §2 lead note
    (R-6).
    """
    lines = [
        "## §2.5 Forward Calendar",
        "",
        "Scheduled US events in the days ahead and the holdings exposed to each. "
        "These are calendar facts, not forecasts.",
        "",
        "| Date | Event | Exposed holdings | Watch |",
        "|------|-------|------------------|-------|",
    ]
    for e in events:
        exposed, watch = _forward_exposure(e, holdings)
        if len(exposed) > 3:
            shown = ", ".join(exposed[:3]) + f" +{len(exposed) - 3}"
        else:
            shown = ", ".join(exposed) if exposed else "—"
        date_str = str(e.get("scheduled_date", ""))
        date_cell = f"{date_str} (today)" if date_str[:10] == report_date_str else date_str
        lines.append(f"| {date_cell} | {e.get('name', '')} | {shown} | {watch} |")
    if _forward_delay_risk(news_items):
        lines += [
            "",
            "> Note: window news references a government funding lapse, which can "
            "delay scheduled BLS/BEA releases; listed dates may slip.",
        ]
    return "\n".join(lines)


def _inject_forward_block(body: str, block: str) -> str:
    """Insert the §2.5 forward block before §3 (#1); fallback before §4 or append."""
    for anchor in ("## §3", "## §4"):
        idx = body.find(anchor)
        if idx != -1:
            return body[:idx].rstrip() + "\n\n" + block + "\n\n" + body[idx:]
    return body.rstrip() + "\n\n" + block


def _build_today_events_block(
    events: list[dict[str, Any]], holdings: list[dict[str, Any]], report_date_str: str
) -> str:
    """Promote events scheduled for the report's own date to a §2 lead note (R-6).

    A forward calendar entry whose date == the report date is happening today, not
    "ahead": leaving it only in the §2.5 forward table understates it. This lifts
    it to the top of §2 as a calendar fact. The report's price data stops at the
    prior close (see R-5), so any same-day release's result is by construction not
    reflected here — stated, never forecast. Empty input → empty string.
    """
    today = [e for e in events if str(e.get("scheduled_date", ""))[:10] == report_date_str]
    if not today:
        return ""
    lines = [
        "**Today's scheduled events** (calendar facts; results not yet in this report's data):"
    ]
    for e in today:
        exposed, watch = _forward_exposure(e, holdings)
        shown = ", ".join(exposed[:3]) + (f" +{len(exposed) - 3}" if len(exposed) > 3 else "")
        tail = f" — exposed: {shown}" if exposed else ""
        lines.append(f"- {e.get('name', '')}{tail}. {watch}")
    return "\n".join(lines)


def _inject_today_events(body: str, block: str) -> str:
    """Insert the today-events note directly under the '## §2' heading (R-6)."""
    anchor = "## §2"
    idx = body.find(anchor)
    if idx == -1:
        return block + "\n\n" + body
    eol = body.find("\n", idx)
    if eol == -1:
        return body + "\n\n" + block
    return body[: eol + 1] + "\n" + block + "\n" + body[eol + 1 :]


# ---------------------------------------------------------------------------
# F3 fixed footer
# ---------------------------------------------------------------------------


def _build_footer(portfolio: dict[str, Any]) -> str:
    """Build the fixed report footer (F3).

    Injected at the template layer — never generated by the LLM.
    Contains: FX rate note (date-stamped from portfolio snapshot) + bilingual disclaimer.
    Wording (both languages) is sourced from i18n_glossary.yml's `templates`
    section (issue #90) — this function only fills in $fx_date/$base_ccy.
    Loads the glossary once (not once per template lookup — PR #91 review).
    """
    base_ccy = portfolio.get("base_currency", "USD")
    fx_date = portfolio.get("fx_date", "unknown")
    templates = load_i18n_glossary().templates

    def tpl(key: str, locale: str) -> str:
        return templates[key][locale]

    def note(locale: str) -> str:
        return Template(tpl("data_sources_note", locale)).substitute(
            fx_date=fx_date, base_ccy=base_ccy
        )

    lines = [
        "",
        "---",
        "",
        f"## {tpl('footer_header', 'en')} / {tpl('footer_header', 'zh-Hans')}",
        "",
        f"**{tpl('data_sources_label', 'en')}** {note('en')}",
        "",
        f"**{tpl('data_sources_label', 'zh-Hans')}** {note('zh-Hans')}",
        "",
        f"**{tpl('disclaimer_label', 'en')}** {tpl('disclaimer', 'en')}",
        "",
        f"**{tpl('disclaimer_label', 'zh-Hans')}** {tpl('disclaimer', 'zh-Hans')}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Data window statement
# ---------------------------------------------------------------------------


def _fx_is_stale(fx_date: str, period_end: str) -> bool:
    """True when the FX rate date trails the window cutoff by more than a day.

    Both are dates we control (fx_rates.rate_date / period_end); a >1-day gap
    means valuations used a materially old rate — worth flagging in a volatile
    week (R-4 surfaced rates frozen 6 days). Any parse failure → not flagged.
    """
    try:
        fx = date.fromisoformat(fx_date[:10])
        end = datetime.fromisoformat(period_end).astimezone(ET).date()
    except (ValueError, TypeError):
        return False
    return (end - fx).days > 1


def _build_data_window(
    news_items: list[dict[str, Any]],
    portfolio: dict[str, Any],
    period_start: str,
    period_end: str,
    trading_days: int,
    price_data_through: str = "",
) -> str:
    """A one-line statement of the intel/data interval this report covers (#5/R-5)."""
    if period_start and period_end:
        ps_et = datetime.fromisoformat(period_start).astimezone(ET).strftime("%Y-%m-%d %H:%M")
        pe_et = datetime.fromisoformat(period_end).astimezone(ET).strftime("%Y-%m-%d %H:%M")
        span = f"{ps_et} to {pe_et} ET"
        td = f"{trading_days} trading day(s)"
        window_line = f"since last report: {span} ({td})"
    else:
        window_line = "window unavailable"
    fx_date = portfolio.get("fx_date", "n/a")
    # R-5: state where PRICE data actually stops. The capture layer only takes
    # closes at session nodes, so a premarket/intraday run has no quotes past the
    # prior close — say so rather than letting the wall-clock window imply it.
    price_line = (
        f" Price data through the {price_data_through} close (session-close "
        "snapshots only — no premarket or intraday quotes)."
        if price_data_through
        else ""
    )
    fx_flag = (
        " [!] FX rate is stale relative to the window cutoff."
        if _fx_is_stale(str(fx_date), period_end)
        else ""
    )
    return (
        f"> **Data window** — {window_line}; {len(news_items)} news item(s); "
        f"FX as of {fx_date}.{price_line} Price moves are measured against the "
        f"baseline close at the window start.{fx_flag}\n\n"
    )


def _header_timestamp(report_date_str: str, period_end: str) -> str:
    """Title timestamp: 'YYYY-MM-DD HH:MM ET' from the window cutoff (#1).

    Falls back to the bare date when period_end is unavailable (e.g. legacy
    re-render of a report that predates the window columns).
    """
    if not period_end:
        return report_date_str
    try:
        end_et = datetime.fromisoformat(period_end).astimezone(ET)
    except ValueError:
        return report_date_str
    return end_et.strftime("%Y-%m-%d %H:%M ET")
