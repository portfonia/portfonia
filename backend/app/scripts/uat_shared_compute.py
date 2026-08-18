"""One-shot production UAT for Ring 1 stage A (issue #128, design doc §7.3).

Inserts the three synthetic users from Hermes/Portfonia/Docs/Ring 1-A design.md
§7.1, fans out generate_report the same way generate_incremental_report does
(shared moves_cache, shared now, fair-share users_remaining), runs the A4
shadow comparison in-process, prints cost / cache / leak / body evidence,
then deletes only those synthetic rows.

Never call generate_incremental_report — that would iterate every real
active user. Never write SHARED_COMPUTE_ENABLED or ASSEMBLY_* into .env;
runtime patches stay inside this process. Never delete ticker_intel /
macro_event_intel / search_cache.

If THIS user has no prior report, the period is one complete trading week
(five weekdays back, ET midnight) — not BOOTSTRAP_WATERMARK, not another
user's watermark.

    docker compose exec backend python -m app.scripts.uat_shared_compute \
        --mid-model <mid-tier model id>
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.timezones import ET
from app.models.holding import Holding
from app.models.macro_event_intel import MacroEventIntel
from app.models.news import News
from app.models.news_surfaced import NewsSurfaced
from app.models.report import Report
from app.models.ticker_intel import TickerIntel
from app.services.report_generator import generate_report, regenerate_report
from app.services.window_data import MovesCache

logger = logging.getLogger(__name__)

# Distinct from the pytest fixture (…00a1/a2/a3) so production rows and
# test-db rows can never be confused when reading logs or leftover data.
U1_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
U2_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
U3_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
UAT_USER_IDS: tuple[uuid.UUID, uuid.UUID, uuid.UUID] = (
    U1_USER_ID,
    U2_USER_ID,
    U3_USER_ID,
)
UAT_LABELS: dict[uuid.UUID, str] = {
    U1_USER_ID: "U1",
    U2_USER_ID: "U2",
    U3_USER_ID: "U3",
}

# Exclusive identifiers used for the zero-leak check (design doc UAT-1).
_U1_EXCLUSIVE = ("QQQM", "110011")
_U2_EXCLUSIVE = ("0700.HK",)
_U3_EXCLUSIVE = ("SGOL", "019547", "513650.SS")
_U1U2_SHARED = ("NVDA", "AAPL")
_TRACKED_IDENTIFIERS = (
    "NVDA",
    "AAPL",
    "QQQM",
    "110011",
    "0700.HK",
    "513650.SS",
    "019547",
    "SGOL",
)

_WATERMARK_SESSION_NODE = "uat_watermark"


class CleanupGuardError(RuntimeError):
    """Raised when a to-be-deleted row is not owned by a synthetic UAT user."""


class _NoOpEmail:
    """Callable stand-in that logs and never talks to Resend/GitHub."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> bool:
        self.calls += 1
        logger.info("uat email guard: swallowed %s (args=%s kwargs=%s)", self.name, args, kwargs)
        return True


def seed_holdings(session: Session) -> None:
    """Insert the §7.1 three-user fixture via the ORM (Fernet encrypts writes)."""

    def _h(**kwargs: object) -> Holding:
        defaults: dict[str, object] = {"pricing_mode": "auto", "currency": "USD"}
        return Holding(**{**defaults, **kwargs})

    session.add_all(
        [
            _h(
                user_id=U1_USER_ID,
                name="NVIDIA",
                ticker="NVDA",
                asset_class="EQUITY_US_TECH",
                shares=Decimal("10"),
            ),
            _h(
                user_id=U1_USER_ID,
                name="Apple",
                ticker="AAPL",
                asset_class="STOCK",
                shares=Decimal("15"),
            ),
            _h(
                user_id=U1_USER_ID,
                name="Invesco QQQM",
                ticker="QQQM",
                asset_class="EQUITY_US_BROAD",
                shares=Decimal("20"),
            ),
            _h(
                user_id=U1_USER_ID,
                name="Offshore Fund",
                fund_code="110011",
                currency="CNY",
                asset_class="EQUITY_CN",
                shares=Decimal("1000"),
            ),
            _h(
                user_id=U1_USER_ID,
                name="Cash",
                pricing_mode="manual",
                asset_class="CASH_EQUIV",
                current_value=Decimal("10000"),
            ),
            _h(
                user_id=U2_USER_ID,
                name="NVIDIA",
                ticker="NVDA",
                asset_class="EQUITY_US_TECH",
                shares=Decimal("8"),
            ),
            _h(
                user_id=U2_USER_ID,
                name="Apple",
                ticker="AAPL",
                asset_class="EQUITY_US_TECH",
                shares=Decimal("12"),
            ),
            _h(
                user_id=U2_USER_ID,
                name="Tencent",
                ticker="0700.HK",
                currency="HKD",
                asset_class="EQUITY_CN",
                shares=Decimal("100"),
            ),
            _h(
                user_id=U2_USER_ID,
                name="USD Cash",
                pricing_mode="manual",
                asset_class="CASH_EQUIV",
                current_value=Decimal("5000"),
            ),
            _h(
                user_id=U3_USER_ID,
                name="CSI 500 ETF",
                ticker="513650.SS",
                currency="CNY",
                asset_class="EQUITY_BROAD",
                shares=Decimal("2000"),
            ),
            _h(
                user_id=U3_USER_ID,
                name="Gold Fund",
                fund_code="019547",
                currency="CNY",
                asset_class="PRECIOUS_METALS",
                shares=Decimal("3000"),
            ),
            _h(
                user_id=U3_USER_ID,
                name="Gold ETF",
                ticker="SGOL",
                asset_class="PRECIOUS_METALS",
                shares=Decimal("40"),
            ),
        ]
    )


def one_trading_week_start(now: datetime) -> datetime:
    """ET midnight five weekdays before `now` — one complete trading week.

    Used when THIS user has no prior report. Do not borrow another user's
    watermark. Saturday/Sunday are skipped so a Monday run lands on the
    previous Monday, not the intervening weekend.
    """
    cursor = now.astimezone(ET).date()
    remaining = 5
    while remaining > 0:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return datetime(cursor.year, cursor.month, cursor.day, tzinfo=ET)


def seed_window_alignment(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Cold-start the three synthetic users onto a one-trading-week window.

    Product rule (2026-08-17): if THIS user has no prior report, the period
    is one complete trading week — not BOOTSTRAP_WATERMARK (2026-06-01) and
    not some other user's watermark.

    News selection has no published_at lower bound (issue #30); without a
    ledger the first report would still ingest the entire capture table.
    Mark news published at or before the week start as already surfaced for
    these synthetic users only, so the visible news set matches the week.
    """
    batch_now = now if now is not None else datetime.now(tz=UTC)
    week_start = one_trading_week_start(batch_now)
    seeded_reports: list[Report] = []
    for user_id in UAT_USER_IDS:
        row = Report(
            user_id=user_id,
            report_date=week_start.astimezone(ET).date(),
            report_type="incremental",
            session_node=_WATERMARK_SESSION_NODE,
            status="success",
            report_md="uat watermark seed — not a real report",
            report_inputs={"uat_seed": True, "source": "one_trading_week"},
            period_start=week_start,
            period_end=week_start,
        )
        session.add(row)
        seeded_reports.append(row)
    session.flush()

    prior_news_ids = list(
        session.execute(select(News.id).where(News.published_at <= week_start)).scalars()
    )
    marked = 0
    for news_id in prior_news_ids:
        for seed in seeded_reports:
            session.add(
                NewsSurfaced(
                    user_id=seed.user_id,
                    news_id=news_id,
                    report_id=seed.id,
                )
            )
            marked += 1
    session.flush()
    return {
        "aligned": True,
        "source": "one_trading_week",
        "watermark_end": week_start.isoformat(),
        "marked_news": marked,
    }


def install_email_guards() -> Callable[[], None]:
    """Hard-disable outbound notify in this process. Env flags are not enough."""
    stack = ExitStack()
    targets = (
        "app.services.report_generator.send_report_email",
        "app.services.report_generator.send_ops_alert",
        "app.services.report_generator.create_bug_report",
        "app.services.ticker_intel.send_ops_alert",
        "app.services.macro_event_intel.send_ops_alert",
    )
    for target in targets:
        stack.enter_context(patch(target, _NoOpEmail(target)))
    return stack.close


def apply_runtime_settings(settings: Any, *, cheap: str, mid: str) -> Callable[[], None]:
    """Patch shadow models in-process. Do not persist, do not flip the master switch."""
    previous_enabled = settings.SHARED_COMPUTE_ENABLED
    previous_shadow = settings.ASSEMBLY_SHADOW_MODELS
    settings.SHARED_COMPUTE_ENABLED = False
    settings.ASSEMBLY_SHADOW_MODELS = f"{cheap},{mid}"

    def restore() -> None:
        settings.SHARED_COMPUTE_ENABLED = previous_enabled
        settings.ASSEMBLY_SHADOW_MODELS = previous_shadow

    return restore


def run_synthetic_batch(
    session: Session,
    *,
    now: datetime | None = None,
    output_lang: str | None = None,
) -> dict[uuid.UUID, Report]:
    """Fan out generate_report to the three synthetic users only.

    Mirrors generate_incremental_report's shared-cache / shared-now /
    users_remaining wiring without iterating active_user_ids().
    """
    settings = get_settings()
    batch_now = now if now is not None else datetime.now(tz=UTC)
    lang = output_lang if output_lang is not None else settings.OUTPUT_LANG
    moves_cache: MovesCache = {}
    reports: dict[uuid.UUID, Report] = {}
    for index, user_id in enumerate(UAT_USER_IDS):
        remaining = len(UAT_USER_IDS) - index
        try:
            report = generate_report(
                session,
                report_type="incremental",
                output_lang=lang,
                session_node="manual",
                user_id=user_id,
                moves_cache=moves_cache,
                now=batch_now,
                users_remaining=remaining,
            )
            reports[user_id] = report
            logger.info(
                "uat: generate_report done user=%s report=%s status=%s",
                user_id,
                report.id,
                report.status,
            )
        except Exception:
            logger.exception("uat: generate_report failed for %s — continuing", user_id)
            session.rollback()
    return reports


def classify_llm_calls(
    calls: list[dict[str, Any]],
    *,
    primary: str,
    cheap: str,
    mid: str,
) -> dict[str, list[dict[str, Any]]]:
    """Split one report's llm_calls by pipeline position, not just model name.

    Order inside generate_report is Pass 1 (cheap) → L1/L2 (cheap) → Pass 2
    (primary) → shadow assembly (cheap, then mid). Mid may equal primary.
    """
    split: dict[str, list[dict[str, Any]]] = {
        "pass1": [],
        "shared_intel": [],
        "pass2": [],
        "shadow_cheap": [],
        "shadow_mid": [],
        "other": [],
    }
    pass1_done = False
    pass2_done = False
    for call in calls:
        model = str(call.get("model") or "")
        if not pass1_done:
            split["pass1"].append(call)
            pass1_done = True
            continue
        if not pass2_done:
            if model == primary:
                split["pass2"].append(call)
                pass2_done = True
            else:
                split["shared_intel"].append(call)
            continue
        if model == mid and (model != cheap or split["shadow_cheap"]):
            split["shadow_mid"].append(call)
        elif model == cheap:
            split["shadow_cheap"].append(call)
        elif model == mid:
            split["shadow_mid"].append(call)
        else:
            split["other"].append(call)
    return split


def _blob_contains(blob: str, token: str) -> bool:
    return token.lower() in blob.lower()


def _report_blob(report_md: str, report_inputs: object) -> str:
    try:
        dumped = json.dumps(report_inputs, default=str)
    except TypeError:
        dumped = str(report_inputs)
    return f"{report_md}\n{dumped}"


def find_cross_user_leaks(reports: dict[uuid.UUID, dict[str, Any]]) -> list[str]:
    """U1/U3 (and U2 exclusives) must not appear in each other's outputs."""
    leaks: list[str] = []
    blobs = {
        uid: _report_blob(str(payload.get("report_md") or ""), payload.get("report_inputs"))
        for uid, payload in reports.items()
    }
    checks: list[tuple[uuid.UUID, tuple[str, ...], str]] = [
        (U3_USER_ID, _U1_EXCLUSIVE + _U1U2_SHARED, "U3"),
        (U1_USER_ID, _U3_EXCLUSIVE, "U1"),
        (U2_USER_ID, _U3_EXCLUSIVE + _U1_EXCLUSIVE, "U2"),
        (U3_USER_ID, _U2_EXCLUSIVE, "U3"),
        (U1_USER_ID, _U2_EXCLUSIVE, "U1"),
    ]
    for uid, tokens, label in checks:
        blob = blobs.get(uid)
        if blob is None:
            continue
        for token in tokens:
            if _blob_contains(blob, token):
                leaks.append(f"{label} contains foreign identifier {token}")
    return leaks


def _assert_synthetic(rows: list[Any], kind: str) -> None:
    for row in rows:
        user_id = getattr(row, "user_id", None)
        if user_id not in UAT_USER_IDS:
            raise CleanupGuardError(
                f"{kind} row {getattr(row, 'id', '?')} has user_id={user_id}, "
                "which is not a synthetic UAT user — refusing to delete"
            )


def cleanup_synthetic_rows(
    session: Session,
    *,
    holding_ids_override: list[uuid.UUID] | None = None,
    report_ids_override: list[uuid.UUID] | None = None,
    news_ids_override: list[uuid.UUID] | None = None,
) -> dict[str, int]:
    """SELECT, verify every row belongs to b1/b2/b3, then delete.

    ticker_intel / macro_event_intel / search_cache are intentionally not
    queried — those tables are global and may already serve the real user.
    """
    if holding_ids_override is not None:
        holdings = list(
            session.execute(select(Holding).where(Holding.id.in_(holding_ids_override))).scalars()
        )
    else:
        holdings = list(
            session.execute(select(Holding).where(Holding.user_id.in_(UAT_USER_IDS))).scalars()
        )
    _assert_synthetic(holdings, "holdings")

    if report_ids_override is not None:
        reports = list(
            session.execute(select(Report).where(Report.id.in_(report_ids_override))).scalars()
        )
    else:
        reports = list(
            session.execute(select(Report).where(Report.user_id.in_(UAT_USER_IDS))).scalars()
        )
    _assert_synthetic(reports, "reports")

    if news_ids_override is not None:
        marks = list(
            session.execute(
                select(NewsSurfaced).where(NewsSurfaced.id.in_(news_ids_override))
            ).scalars()
        )
    else:
        marks = list(
            session.execute(
                select(NewsSurfaced).where(NewsSurfaced.user_id.in_(UAT_USER_IDS))
            ).scalars()
        )
    _assert_synthetic(marks, "news_surfaced")

    print(
        f"[uat] cleanup SELECT: holdings={len(holdings)} "
        f"reports={len(reports)} news_surfaced={len(marks)} "
        f"user_ids={sorted({str(r.user_id) for r in holdings + reports + marks})}"
    )

    for mark_row in marks:
        session.delete(mark_row)
    for report_row in reports:
        session.delete(report_row)
    for holding_row in holdings:
        session.delete(holding_row)
    return {
        "holdings": len(holdings),
        "reports": len(reports),
        "news_surfaced": len(marks),
    }


def leftover_counts(session: Session) -> dict[str, int]:
    return {
        "holdings": len(
            list(
                session.execute(select(Holding).where(Holding.user_id.in_(UAT_USER_IDS))).scalars()
            )
        ),
        "reports": len(
            list(session.execute(select(Report).where(Report.user_id.in_(UAT_USER_IDS))).scalars())
        ),
        "news_surfaced": len(
            list(
                session.execute(
                    select(NewsSurfaced).where(NewsSurfaced.user_id.in_(UAT_USER_IDS))
                ).scalars()
            )
        ),
    }


def snapshot_shared_cache(session: Session, trade_date: Any) -> dict[str, dict[str, Any]]:
    l1_rows = list(
        session.execute(
            select(TickerIntel).where(
                TickerIntel.identifier.in_(_TRACKED_IDENTIFIERS),
                TickerIntel.trade_date == trade_date,
            )
        ).scalars()
    )
    l2_rows = list(
        session.execute(
            select(MacroEventIntel).where(MacroEventIntel.trade_date == trade_date)
        ).scalars()
    )
    return {
        "l1": {
            row.identifier: {
                "has_analysis": row.analysis is not None,
                "attempt_count": row.attempt_count,
                "id": str(row.id),
            }
            for row in l1_rows
        },
        "l2": {
            row.event_key: {
                "has_analysis": row.analysis is not None,
                "attempt_count": getattr(row, "attempt_count", None),
                "id": str(row.id),
            }
            for row in l2_rows
        },
    }


def _sum_cost(calls: list[dict[str, Any]]) -> float:
    total = 0.0
    for call in calls:
        cost = call.get("cost")
        if isinstance(cost, (int, float)):
            total += float(cost)
    return total


def _sum_tokens(calls: list[dict[str, Any]], key: str) -> int:
    return sum(int(c[key]) for c in calls if isinstance(c.get(key), int))


def verify_rerender_zero_llm(session: Session, report: Report) -> dict[str, Any]:
    """Re-run mode=render as the report's owner; count every _call_llm site."""
    calls = {"generator": 0, "assembly": 0, "translation": 0}

    def _counter(bucket: str) -> Callable[..., str]:
        def _inner(*_args: Any, **_kwargs: Any) -> str:
            calls[bucket] += 1
            raise AssertionError(f"regenerate(mode=render) called {bucket} _call_llm")

        return _inner

    with (
        patch("app.services.report_generator._call_llm", _counter("generator")),
        patch("app.services.report_assembly._call_llm", _counter("assembly")),
        patch("app.services.report_translation._call_llm", _counter("translation")),
        patch("app.services.report_generator.get_current_user_id", lambda: report.user_id),
    ):
        rebuilt = regenerate_report(session, report.id, mode="render", output_lang="en")
    total = sum(calls.values())
    return {
        "report_id": str(rebuilt.id),
        "calls": calls,
        "total": total,
        "ok": total == 0,
        "rebuilt_chars": len(rebuilt.report_md or ""),
    }


def _print_shadow_bodies(label: str, shadow: dict[str, Any], cheap: str, mid: str) -> None:
    print(f"\n===== {label} assembly_shadow / cheap ({cheap}) =====")
    cheap_entry = shadow.get(cheap) or {}
    if "raw" in cheap_entry:
        print(cheap_entry["raw"])
    else:
        print(json.dumps(cheap_entry, ensure_ascii=False, indent=2))
    print(f"\n===== {label} assembly_shadow / mid ({mid}) =====")
    mid_entry = shadow.get(mid) or {}
    if "raw" in mid_entry:
        print(mid_entry["raw"])
    else:
        print(json.dumps(mid_entry, ensure_ascii=False, indent=2))


def _print_summary(payload: dict[str, Any]) -> None:
    print("\n===== UAT SUMMARY =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ring 1 stage A §7.3 production UAT")
    parser.add_argument(
        "--mid-model",
        default="",
        help="Mid-tier ASSEMBLY candidate. Required unless --cleanup-only. Do not guess.",
    )
    parser.add_argument(
        "--cheap-model",
        default="",
        help="Cheap ASSEMBLY candidate. Default: current LOW_COST_LLM_MODEL.",
    )
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument(
        "--force-reset",
        action="store_true",
        help="Delete leftover synthetic rows before seeding a new run.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if args.cleanup_only:
        with SessionLocal() as session:
            print("[uat] leftover before cleanup:", leftover_counts(session))
            deleted = cleanup_synthetic_rows(session)
            session.commit()
            print("[uat] deleted:", deleted)
            print("[uat] leftover after cleanup:", leftover_counts(session))
        return 0

    cheap = (args.cheap_model or settings.LOW_COST_LLM_MODEL).strip()
    mid = args.mid_model.strip()
    if not mid:
        print("[ERR] --mid-model is required (design doc §6.3.1 — do not guess a model).")
        return 2
    if settings.SHARED_COMPUTE_ENABLED:
        print(
            "[ERR] SHARED_COMPUTE_ENABLED is already true in this process. "
            "Aborting — UAT must run against the production default (false)."
        )
        return 2

    restore_email = install_email_guards()
    restore_settings = apply_runtime_settings(settings, cheap=cheap, mid=mid)
    started = time.perf_counter()
    summary: dict[str, Any] = {
        "cheap_model": cheap,
        "mid_model": mid,
        "primary_model": settings.PRIMARY_LLM_MODEL,
        "shared_compute_enabled": bool(settings.SHARED_COMPUTE_ENABLED),
        "output_lang": settings.OUTPUT_LANG,
    }

    try:
        with SessionLocal() as session:
            leftover = leftover_counts(session)
            summary["leftover_at_start"] = leftover
            if any(leftover.values()):
                if not args.force_reset:
                    print(
                        f"[ERR] leftover synthetic rows present: {leftover}. Re-run with --force-reset."
                    )
                    return 2
                cleanup_synthetic_rows(session)
                session.commit()

            seed_holdings(session)
            batch_now = datetime.now(tz=UTC)
            alignment = seed_window_alignment(session, now=batch_now)
            session.commit()
            summary["window_alignment"] = alignment

            trade_date = batch_now.astimezone(ET).date()
            cache_before = snapshot_shared_cache(session, trade_date)
            summary["cache_before"] = cache_before

            reports = run_synthetic_batch(session, now=batch_now)
            elapsed = time.perf_counter() - started
            summary["wall_clock_seconds"] = round(elapsed, 1)
            summary["cache_after"] = snapshot_shared_cache(session, trade_date)

            per_user: dict[str, Any] = {}
            leak_payload: dict[uuid.UUID, dict[str, Any]] = {}
            for user_id, report in reports.items():
                label = UAT_LABELS[user_id]
                inputs = report.report_inputs or {}
                calls = list(inputs.get("llm_calls") or [])
                split = classify_llm_calls(
                    calls,
                    primary=settings.PRIMARY_LLM_MODEL,
                    cheap=cheap,
                    mid=mid,
                )
                per_user[label] = {
                    "report_id": str(report.id),
                    "status": report.status,
                    "body_source": inputs.get("body_source"),
                    "llm_calls": split,
                    "llm_call_count": len(calls),
                    "cost_total": _sum_cost(calls),
                    "prompt_tokens": _sum_tokens(calls, "prompt_tokens"),
                    "completion_tokens": _sum_tokens(calls, "completion_tokens"),
                    "assembly_shadow_models": sorted((inputs.get("assembly_shadow") or {}).keys()),
                    "ticker_intel_keys": sorted((inputs.get("ticker_intel") or {}).keys()),
                    "macro_event_intel_keys": sorted(
                        (inputs.get("macro_event_intel") or {}).keys()
                    ),
                    "stale_tickers": (inputs.get("portfolio_summary") or {}).get("stale_tickers"),
                }
                leak_payload[user_id] = {
                    "report_md": report.report_md or "",
                    "report_inputs": inputs,
                }
                if label == "U1":
                    _print_shadow_bodies(
                        "U1",
                        inputs.get("assembly_shadow") or {},
                        cheap,
                        mid,
                    )

            summary["users"] = per_user
            leaks = find_cross_user_leaks(leak_payload)
            summary["leaks"] = leaks
            summary["zero_leak"] = leaks == []

            l1_fresh: dict[str, Any] = {}
            for ident in _TRACKED_IDENTIFIERS:
                before = cache_before["l1"].get(ident)
                after = summary["cache_after"]["l1"].get(ident)
                l1_fresh[ident] = {
                    "before": before,
                    "after": after,
                    "new_row": before is None and after is not None,
                    "analysis_added": (before is None or not before.get("has_analysis"))
                    and bool(after and after.get("has_analysis")),
                }
            summary["l1_identifier_delta"] = l1_fresh

            if reports:
                first = next(iter(reports.values()))
                summary["rerender"] = verify_rerender_zero_llm(session, first)

            print(f"[uat] leaks: {leaks or 'none'}")
            _print_summary(summary)

            deleted = cleanup_synthetic_rows(session)
            session.commit()
            summary["deleted"] = deleted
            leftover_after = leftover_counts(session)
            summary["leftover_at_end"] = leftover_after
            print("[uat] cleanup:", deleted, "leftover:", leftover_after)
            if any(leftover_after.values()):
                print("[ERR] synthetic rows remain after cleanup")
                return 1
        return 0
    finally:
        restore_settings()
        restore_email()


if __name__ == "__main__":
    raise SystemExit(main())
