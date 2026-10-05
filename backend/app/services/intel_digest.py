"""Plain-English collection reports for one attempt of an intelligence batch."""

from __future__ import annotations

import re
from collections import Counter
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.intel import IntelCollectionRun, IntelSlotRun
from app.services.intel_body import without_urls
from app.services.macro_detector import macro_theme_labels


def obj(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def number(value: object) -> float:
    return float(value) if isinstance(value, (float, int)) else 0


def objects(value: object) -> list[dict[str, object]]:
    return [obj(v) for v in value] if isinstance(value, list) else []


SOURCES = {
    "finnhub": "Finnhub",
    "google_news": "Google News",
    "yahoo": "Yahoo",
    "sec": "SEC filings",
    "eastmoney": "Eastmoney filings",
    "rss": "RSS feeds",
    "classifier": "AI review",
    "tavily": "Tavily",
    "parallel": "Parallel",
    "deepening": "Paid deepening",
    "search_filter": "Headline checks",
    "slot": "Batch",
    "profile": "Company names",
    "collection": "News collection",
    "cleaning": "Headline checks",
    "L1": "Instrument briefs",
    "L2": "Macro-event notes",
    "L3": "Cross-instrument themes",
}
SOURCE_LINES = {
    "finnhub": "Finnhub ...........",
    "google_news": "Google News .......",
    "yahoo": "Yahoo ...............",
    "sec": "SEC filings ...........",
    "eastmoney": "Eastmoney filings .....",
}
DROPS = (
    ("Do not name the company ................", ("unrelated_rule", "unrelated_llm")),
    ("Already collected in an earlier batch ....", ("duplicate_earlier",)),
    ("Same story as another headline, reworded .", ("duplicate", "duplicate_llm")),
    ("Stock picks, promotion, routine price recaps", ("low_value_rule", "promo_llm")),
    ("Old news republished with a new date .....", ("stale_rule", "stale_llm")),
    ("Not an article page ......................", ("non_article",)),
)
MARKETS = {"HK": "Hong Kong", "A-Share": "China A-share"}


def problem_lines(errors: list[str]) -> list[str]:
    if not errors:
        return ["Problems: none"]
    counts: Counter[tuple[str, str]] = Counter()
    for error in errors:
        prefix = error.split(":", 1)[0].split(" ", 1)[0]
        source = SOURCES.get(prefix, "News source")
        http = re.search(r"HTTP (\d{3})", error)
        if error.startswith("classifier:"):
            reason = "AI review failed" + (f" (HTTP {http[1]})" if http else "")
        elif "key not set" in error:
            reason = "API key is not configured"
        elif "invalid_key" in error:
            reason = "API key is invalid"
        elif "quota_or_rate" in error:
            reason = "provider quota or request limit reached"
        elif http:
            reason = f"request failed (HTTP {http[1]})"
        elif "timeout" in error.lower():
            reason = "timed out"
        elif "classifier_failed" in error:
            reason = "AI review failed"
        else:
            exception = re.search(r"\b[A-Za-z]+(?:Error|Exception)\b", error)
            reason = f"unexpected error ({exception[0] if exception else 'Error'})"
        counts[source, reason] += 1
    return [
        "Problems:",
        *[f"  {source}: {reason} ({count} times)" for (source, reason), count in counts.items()],
    ]


def errors_from(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def batch_subject(slot: IntelSlotRun, *, failed: bool = False) -> str:
    stamp = slot.started_at.astimezone(ET).strftime("%a %Y-%m-%d %H:%M")
    trigger = "manual" if slot.details.get("trigger") == "manual" else "scheduled"
    status = (
        "Failed"
        if failed or slot.status == "failed"
        else "OK"
        if slot.status == "ok"
        else "Partial"
    )
    return f"[Portfonia] Intel batch {stamp} ET ({trigger}) - {status}"


def build_batch_report(
    session: Session, slot: IntelSlotRun
) -> tuple[str, str, Literal["INFO", "WARNING"]]:
    runs = list(
        session.scalars(
            select(IntelCollectionRun)
            .where(
                IntelCollectionRun.slot_run_id == slot.id,
                IntelCollectionRun.started_at >= slot.started_at,
            )
            .order_by(IntelCollectionRun.started_at)
        )
    )
    severity: Literal["INFO", "WARNING"] = "WARNING" if slot.status == "failed" else "INFO"
    totals: dict[str, Counter[str]] = {}
    markets: dict[str, Counter[str]] = {}
    cleaning: Counter[str] = Counter()
    classifier: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    unreached: dict[str, list[str]] = {}
    free_errors: list[str] = []
    rss_items = rss_errors = rss_feeds = 0
    duration = 0.0
    for run in runs:
        free_errors.extend(run.errors)
        if run.status == "failed":
            severity = "WARNING"
        if run.kind == "rss":
            feeds = obj(run.stats.get("feeds"))
            rss_feeds += len(feeds)
            rss_items += int(sum(number(obj(v).get("items")) for v in feeds.values()))
            rss_errors += int(sum(number(obj(v).get("errors")) for v in feeds.values()))
            rss = obj(run.stats.get("rss"))
            if number(rss.get("calls")) and number(rss.get("errors")) >= number(rss.get("calls")):
                severity = "WARNING"
            continue
        if run.finished_at:
            duration += (run.finished_at - run.started_at).total_seconds()
        for source in SOURCE_LINES:
            source_values = obj(run.stats.get(source))
            target = totals.setdefault(source, Counter())
            target.update({k: number(v) for k, v in source_values.items()})
            if number(source_values.get("calls")) and number(source_values.get("errors")) >= number(
                source_values.get("calls")
            ):
                severity = "WARNING"
        for market, raw in obj(run.stats.get("markets")).items():
            markets.setdefault(market, Counter()).update(
                {k: number(v) for k, v in obj(raw).items()}
            )
        for market, raw in obj(run.stats.get("unreached")).items():
            if isinstance(raw, list):
                unreached.setdefault(market, []).extend(str(v) for v in raw)
        cleaning.update({k: number(v) for k, v in obj(run.stats.get("cleaning")).items()})
        classifier.update({k: number(v) for k, v in obj(run.stats.get("classifier")).items()})
        for reason, raw in obj(run.stats.get("cleaning_samples")).items():
            if isinstance(raw, list):
                samples.setdefault(reason, []).extend(str(v) for v in raw)
    processed = sum(m["processed"] for m in markets.values())
    total = sum(m["total"] for m in markets.values())
    coverage = ", ".join(f"{MARKETS.get(k, k)} {v['processed']:g}" for k, v in markets.items())
    lines = [
        "PART 1 - FREE NEWS COLLECTION",
        f"Instruments covered: {processed:g} of {total:g}"
        + (f" ({coverage})" if coverage else "")
        + f". Took {duration:.0f} seconds.",
    ]
    for market, identifiers in unreached.items():
        lines.append(
            f"Not reached in the time limit: {MARKETS.get(market, market)}: {','.join(dict.fromkeys(identifiers))}."
        )
    lines += ["", "Headlines fetched (published in the last 48 hours):"]
    for source, label in SOURCE_LINES.items():
        values = totals.get(source, Counter())
        if source == "google_news" and values["calls"] == 0:
            continue
        lines.append(f"  {label} {values['items']:,.0f} ({values['calls']:g} requests)")
        if values["skipped_no_name"]:
            lines.append(
                f"  Not queried (no company name on file): {SOURCES[source]} for {values['skipped_no_name']:g} instruments."
            )
    lines.append(
        f"  RSS feeds ({rss_feeds}) ...... {rss_items:,}, "
        + (f"{rss_errors} errors" if rss_errors else "no errors")
    )
    deep = obj(slot.details.get("deepening"))
    for metric in obj(deep.get("metrics")).values():
        filtered = obj(obj(metric).get("search_filtered"))
        for reason in ("stale_rule", "stale_llm", "stale_lookup_failed"):
            cleaning[reason] += int(number(filtered.get(reason)))
    for reason, raw in obj(deep.get("search_samples")).items():
        if isinstance(raw, list):
            samples.setdefault(reason, []).extend(str(v) for v in raw)
    lines += ["", "Headlines dropped:"]
    for label, reasons in DROPS:
        count = sum(cleaning[r] for r in reasons)
        titles = list(dict.fromkeys(without_urls(t) for r in reasons for t in samples.get(r, [])))[
            :3
        ]
        lines.append(
            f"  {label} {count:,.0f}"
            + ("  e.g. " + "; ".join(f'"{t}"' for t in titles) if titles else "")
        )
    inserted = sum(t["inserted"] for t in totals.values())
    lines += [
        "",
        f"Kept: {cleaning['kept'] - cleaning['filings_stored']:g} headlines and {cleaning['filings_stored']:g} company filings ({inserted:g} of them new to the database).",
        f"AI review: checked {classifier['items']:g} headlines in {classifier['batches']:g} calls, cost ${classifier['cost_usd']:.3f}, {classifier['failed_batches']:g} failed calls; {cleaning['stored_null_label']:g} kept without a label.",
        *(
            [
                f"Earnings-date check failed for {cleaning['stale_lookup_failed']:,.0f} earnings-recap headlines; they were kept."
            ]
            if cleaning["stale_lookup_failed"]
            else []
        ),
        *problem_lines(free_errors),
        "",
        "PART 2 - PAID DEEPENING",
    ]
    deep = obj(slot.details.get("deepening"))
    labels = macro_theme_labels()

    def unit_name(unit: dict[str, object]) -> str:
        return str(unit.get("identifier") or labels.get(str(unit.get("theme")), "Macro theme"))

    picked: dict[str, list[str]] = {}
    for unit in objects(deep.get("selections")):
        reason = str(unit.get("reason", ""))
        if unit.get("kind") == "macro":
            theme_count = re.search(r"theme (\d+) items", reason)
            words = f"{theme_count[1] if theme_count else '0'} matching headlines"
        elif unit.get("kind") == "mover":
            words = f"price move ({reason})"
        elif reason == "new_filing":
            words = "new company filing"
        elif reason.startswith("news_spike "):
            words = f"unusual news volume ({reason.split(' ', 1)[1]} usual)"
        else:
            words = "close to the multi-day move threshold"
        picked.setdefault(words, []).append(unit_name(unit))
    lines.append(
        "Picked: "
        + (
            "; ".join(f"{', '.join(names)} ({reason})" for reason, names in picked.items())
            if picked
            else "none"
        )
    )
    outcomes = objects(deep.get("outcomes"))
    kept_units = {unit_name(o) for o in outcomes if number(o.get("accepted"))}
    for outcome in outcomes:
        name = unit_name(outcome)
        accepted = number(outcome.get("accepted"))
        # The "nothing usable" line names no provider, so skip it for a unit that kept
        # articles through another provider (search fallback or A/B sibling).
        if not accepted and outcome.get("note") != "no_news" and name in kept_units:
            continue
        provider = "Tavily" if outcome.get("provider") == "tavily" else "Parallel"
        if accepted:
            via = "headline search" if outcome.get("via") == "search" else "direct links"
            lines.append(f"  {name}  {provider}  {accepted:g} articles kept ({via})")
        elif outcome.get("note") == "no_news":
            lines.append(f"  {name}  no news to follow up, no paid call")
        else:
            reason = (
                "batch limit reached"
                if outcome.get("note") == "cap_reached"
                else "no usable article found"
            )
            lines.append(f"  {name}  nothing usable ({reason})")
    metrics = {p: obj(v) for p, v in obj(deep.get("metrics")).items()}
    kept = sum(number(m.get("accepted")) for m in metrics.values())
    failed = sum(number(obj(m.get("rejected")).get("provider_error")) for m in metrics.values())
    rejected = sum(
        sum(number(n) for r, n in obj(m.get("rejected")).items() if r != "provider_error")
        for m in metrics.values()
    )
    usage = obj(deep.get("usage"))
    tavily, parallel = obj(usage.get("tavily")), obj(usage.get("parallel"))
    lines += [
        f"Articles kept: {kept:g}. Rejected: {rejected:g}. Failed: {failed:g}.",
        f"Spend this batch: Tavily {number(tavily.get('run')):g} credits, Parallel ${number(parallel.get('run')):.3f}.",
    ]
    tused, tlimit = number(tavily.get("month")), number(tavily.get("month_limit"))
    pused, plimit = number(parallel.get("month")), number(parallel.get("month_limit"))
    lines.append(
        f"Spend this month: Tavily {tused:g} of {tlimit:,.0f} credits ({tused / tlimit * 100 if tlimit else 0:.0f}%), Parallel ${pused:.2f} of ${plimit:.2f} ({pused / plimit * 100 if plimit else 0:.0f}%)."
    )
    costs = []
    for provider in ("tavily", "parallel"):
        metric = metrics.get(provider, {})
        accepted = number(metric.get("accepted"))
        costs.append(
            f"${number(metric.get('cost_usd')) / accepted:.4f}" if accepted else "not available"
        )
    lines.append(f"Tavily vs Parallel: cost per kept article {costs[0]} vs {costs[1]}.")
    shared = obj(slot.details.get("shared_analysis"))
    if slot.slot == "post_close" and slot.run_date.weekday() < 5:
        lines.append(
            f"Analysis written: {number(shared.get('l1_written')):g} instrument briefs ({number(shared.get('l1_cache_hits')):g} reused), {number(shared.get('l2_written')):g} macro-event notes, {number(shared.get('l3_clusters')):g} cross-instrument themes."
        )
    deep_errors = errors_from(slot.details.get("deepening_errors")) or errors_from(
        deep.get("errors")
    )
    analysis_errors = errors_from(shared.get("errors"))
    if deep_errors or analysis_errors:
        severity = "WARNING"
    lines += problem_lines(deep_errors + analysis_errors)
    return batch_subject(slot), "\n".join(lines), severity
