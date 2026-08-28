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
from app.models.account import Account
from app.models.holding import Holding
from app.models.macro_event_intel import MacroEventIntel
from app.models.news import News
from app.models.news_surfaced import NewsSurfaced
from app.models.report import Report
from app.models.ticker_intel import TickerIntel
from app.models.user import User
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

# Holdings-derived report_inputs keys only. ticker_intel / holding_news
# values are L1/headline prose and are not scanned (PR #164 review round 4).

EMAIL_GUARD_TARGETS: tuple[str, ...] = (
    "app.services.report_generator.send_report_email",
    "app.services.report_generator.send_ops_alert",
    "app.services.report_generator.create_bug_report",
    "app.services.ticker_intel.send_ops_alert",
    "app.services.macro_event_intel.send_ops_alert",
)

_BEAT_WEEKDAYS = {0, 2, 4}  # Mon / Wed / Fri
_BEAT_HOUR = 17
_BEAT_MINUTE = 0
_BEAT_GUARD_MINUTES = 30
# Three Pass 2 + six shadow + zh translation can run past 17:00 if we only
# guard ±30 min around the Beat instant. Block from 15:30 ET (90 min lead).
_BEAT_LEAD_MINUTES = 90


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


def seed_synthetic_users(session: Session) -> None:
    """Idempotent: `holdings.user_id` now FKs to `users.id` (issue #129 B7)
    — the three synthetic UAT ids need a real row before `seed_holdings`
    can write anything under them. Get-or-create since this script may run
    more than once against a database that already has them (a prior run's
    `cleanup_synthetic_rows` call, below, removes them again)."""
    for user_id in UAT_USER_IDS:
        if session.get(User, user_id) is not None:
            continue
        session.add(
            User(
                id=user_id,
                auth_provider="supabase",
                auth_subject=f"uat-{user_id}",
                email=f"uat-{user_id}@portfonia.invalid",
                status="active",
                locale="zh",
                base_currency="USD",
                report_cadence="mwf",
            )
        )
    session.flush()


def seed_holdings(session: Session) -> None:
    """Insert the §7.1 three-user fixture via the ORM (Fernet encrypts writes)."""
    seed_synthetic_users(session)

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

    Delegates to the production cold-start helper so UAT and live reports
    cannot drift (Ring 1-B §6.6).
    """
    from app.services.window_data import cold_start_watermark

    return cold_start_watermark(now)


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
    for target in EMAIL_GUARD_TARGETS:
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


def beat_window_blocks(now: datetime) -> bool:
    """True if a run starting now could still be in the Beat fire window.

    Mon/Wed/Fri 15:30-17:30 ET: 90 min lead (expected UAT runtime) plus the
    original ±30 min guard around 17:00. Tuesday 17:00 is not a Beat tick.
    """
    et = now.astimezone(ET)
    if et.weekday() not in _BEAT_WEEKDAYS:
        return False
    scheduled = et.replace(hour=_BEAT_HOUR, minute=_BEAT_MINUTE, second=0, microsecond=0)
    start = scheduled - timedelta(minutes=_BEAT_LEAD_MINUTES)
    end = scheduled + timedelta(minutes=_BEAT_GUARD_MINUTES)
    return start <= et <= end


def keep_reports_blocked(_now: datetime | None = None) -> bool:
    """--keep-reports always needs --i-know: leftovers survive until the next Beat."""
    return True


_DONE_STATUSES = frozenset({"success", "needs_review", "skipped"})
_REQUIRED_LABELS = frozenset({"U1", "U2", "U3"})


def failed_run(summary: dict[str, Any]) -> bool:
    if summary.get("leaks"):
        return True
    rerender = summary.get("rerender") or {}
    if rerender.get("ok") is False:
        return True
    users = summary.get("users") or {}
    if set(users) != _REQUIRED_LABELS:
        return True
    return any(str(row.get("status")) not in _DONE_STATUSES for row in users.values())


def report_has_stored_body(report: Any) -> bool:
    """regenerate(mode=render) needs assembly_raw or pass2_raw. Quiet skipped rows have neither."""
    if getattr(report, "status", None) not in ("success", "needs_review"):
        return False
    inputs = getattr(report, "report_inputs", None) or {}
    if not isinstance(inputs, dict):
        return False
    return bool(inputs.get("assembly_raw") or inputs.get("pass2_raw"))


def evidence_blocks(
    label: str,
    report_md: str,
    inputs: dict[str, Any],
    *,
    cheap: str,
    mid: str,
) -> list[tuple[str, str]]:
    """Shipped body plus both shadow raws — the A4 side-by-side read."""
    shadow = inputs.get("assembly_shadow") or {}
    cheap_entry = shadow.get(cheap) or {}
    mid_entry = shadow.get(mid) or {}
    cheap_text = (
        str(cheap_entry.get("raw"))
        if "raw" in cheap_entry
        else json.dumps(cheap_entry, ensure_ascii=False, indent=2)
    )
    mid_text = (
        str(mid_entry.get("raw"))
        if "raw" in mid_entry
        else json.dumps(mid_entry, ensure_ascii=False, indent=2)
    )
    return [
        (f"{label} shipped report_md", report_md or ""),
        (f"{label} pass2_raw", str(inputs.get("pass2_raw") or "")),
        (f"{label} assembly_shadow / cheap ({cheap})", cheap_text),
        (f"{label} assembly_shadow / mid ({mid})", mid_text),
    ]


def _blob_contains(blob: str, token: str) -> bool:
    return token.lower() in blob.lower()


def _report_blob(report_inputs: object) -> str:
    if not isinstance(report_inputs, dict):
        return ""
    pieces: list[str] = []
    portfolio = report_inputs.get("portfolio_summary")
    if portfolio is not None:
        pieces.append(json.dumps(portfolio, default=str))
    anomalies = report_inputs.get("price_anomalies")
    if isinstance(anomalies, list):
        idents: list[object] = []
        for item in anomalies:
            if not isinstance(item, dict):
                continue
            ident = item.get("identifier")
            if ident:
                idents.append(ident)
            constituents = item.get("constituents")
            if isinstance(constituents, list):
                idents.extend(constituents)
        pieces.append(json.dumps(idents, default=str))
    for key in ("ticker_intel", "holding_news", "macro_event_exposure"):
        value = report_inputs.get(key)
        if isinstance(value, dict):
            pieces.append(json.dumps(list(value.keys()), default=str))
    return "\n".join(pieces)


def find_cross_user_leaks(reports: dict[uuid.UUID, dict[str, Any]]) -> list[str]:
    """U1/U3 (and U2 exclusives) must not appear in each other's holdings fields."""
    leaks: list[str] = []
    blobs = {uid: _report_blob(payload.get("report_inputs")) for uid, payload in reports.items()}
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
    session.flush()
    # Accounts next (issue #129 B7 review): holdings.account_id FKs to
    # accounts.id ON DELETE RESTRICT, so this must run after holdings are
    # gone. Not SELECT-by-override like the three above — `seed_holdings`
    # only ever creates accounts for UAT_USER_IDS, so a plain user_id filter
    # is already exact, no override callers need this narrowed.
    accounts = list(
        session.execute(select(Account).where(Account.user_id.in_(UAT_USER_IDS))).scalars()
    )
    _assert_synthetic(accounts, "accounts")
    for account_row in accounts:
        session.delete(account_row)
    session.flush()
    # Users are the last thing removed: holdings/reports/news_surfaced/
    # accounts all FK to users.id ON DELETE RESTRICT, so this must run
    # after all of them are gone. Idempotent — a prior cleanup call in the
    # same process may have already removed them.
    users_deleted = 0
    for user_id in UAT_USER_IDS:
        row = session.get(User, user_id)
        if row is not None:
            session.delete(row)
            users_deleted += 1
    return {
        "holdings": len(holdings),
        "reports": len(reports),
        "news_surfaced": len(marks),
        "accounts": len(accounts),
        "users": users_deleted,
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
        # issue #129 B7 review: cleanup's own proof of completeness was
        # silently incomplete — accounts and users are cleanup's newest two
        # steps and neither was ever included in this leftover check.
        "accounts": len(
            list(
                session.execute(select(Account).where(Account.user_id.in_(UAT_USER_IDS))).scalars()
            )
        ),
        "users": len([uid for uid in UAT_USER_IDS if session.get(User, uid) is not None]),
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


def restore_shipped_body(
    session: Session, report: Report, shipped_md: str | None, shipped_translated: object
) -> None:
    """Put back the shipped body after a mode=render check that commits English."""
    report.report_md = shipped_md
    inputs = report.report_inputs
    if isinstance(inputs, dict):
        report.report_inputs = {**inputs, "pass2_translated": shipped_translated}
    session.commit()


def verify_rerender_zero_llm(session: Session, report: Report) -> dict[str, Any]:
    """Re-run mode=render as the report's owner; count every _call_llm site.

    regenerate commits. Snapshot the shipped zh body and write it back so
    --keep-reports still stores what was actually emailed/printed.
    """
    calls = {"generator": 0, "assembly": 0, "translation": 0}
    shipped_md = report.report_md
    shipped_inputs = report.report_inputs if isinstance(report.report_inputs, dict) else {}
    shipped_translated = shipped_inputs.get("pass2_translated")

    def _counter(bucket: str) -> Callable[..., str]:
        def _inner(*_args: Any, **_kwargs: Any) -> str:
            calls[bucket] += 1
            raise AssertionError(f"regenerate(mode=render) called {bucket} _call_llm")

        return _inner

    rebuilt: Report | None = None
    error: str | None = None
    try:
        with (
            patch("app.services.report_generator._call_llm", _counter("generator")),
            patch("app.services.report_assembly._call_llm", _counter("assembly")),
            patch("app.services.report_translation._call_llm", _counter("translation")),
        ):
            rebuilt = regenerate_report(
                session, report.id, user_id=report.user_id, mode="render", output_lang="en"
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        restore_shipped_body(session, report, shipped_md, shipped_translated)
    total = sum(calls.values())
    ok = error is None and total == 0
    return {
        "report_id": str(rebuilt.id) if rebuilt is not None else str(report.id),
        "calls": calls,
        "total": total,
        "ok": ok,
        "error": error,
        "rebuilt_chars": len(rebuilt.report_md or "") if rebuilt is not None else 0,
        "shipped_md_restored": report.report_md == shipped_md,
    }


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
    parser.add_argument(
        "--keep-reports",
        action="store_true",
        help="Leave synthetic rows in place after the run so evidence can be re-read.",
    )
    parser.add_argument(
        "--i-know",
        action="store_true",
        help="Bypass the Beat safety window and allow --keep-reports.",
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
    now_et = datetime.now(tz=UTC)
    if beat_window_blocks(now_et) and not args.i_know:
        print(
            "[ERR] now is inside the Mon/Wed/Fri Beat safety window "
            "(15:30-17:30 ET). A run starting now can still be in generate "
            "when Celery fires at 17:00 ET and emails synthetic users to "
            "DEV_USER_EMAIL. Wait, or pass --i-know."
        )
        return 2
    if args.keep_reports and keep_reports_blocked(now_et) and not args.i_know:
        print(
            "[ERR] --keep-reports leaves b1/b2/b3 in active_user_ids() until "
            "the next Beat emails them to DEV_USER_EMAIL. Pass --i-know "
            "and run --cleanup-only before that Beat."
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
    seeded = False
    keep = bool(args.keep_reports)
    exit_code = 0

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
            seeded = True
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
                for title, body in evidence_blocks(
                    label, report.report_md or "", inputs, cheap=cheap, mid=mid
                ):
                    print(f"\n===== {title} =====")
                    print(body)

            summary["users"] = per_user
            summary["missing_users"] = sorted(_REQUIRED_LABELS - set(per_user))
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

            rerender_target = next(
                (report for report in reports.values() if report_has_stored_body(report)),
                None,
            )
            if rerender_target is not None:
                summary["rerender"] = verify_rerender_zero_llm(session, rerender_target)
            else:
                summary["rerender"] = {
                    "ok": None,
                    "skipped": "no success/needs_review row with a stored body",
                }

            if keep:
                print(
                    "[uat] --keep-reports: leaving synthetic rows in place. "
                    "Beat will treat these UUIDs as active users until --cleanup-only."
                )
            else:
                deleted = cleanup_synthetic_rows(session)
                session.commit()
                seeded = False
                summary["deleted"] = deleted
                leftover_after = leftover_counts(session)
                summary["leftover_at_end"] = leftover_after
                print("[uat] cleanup:", deleted, "leftover:", leftover_after)
                if any(leftover_after.values()):
                    print("[ERR] synthetic rows remain after cleanup")
                    exit_code = 1

            leftover_after = summary.get("leftover_at_end") or {"holdings": 0}
            cleanup_ok = keep or not any(leftover_after.values()) if leftover_after else True
            if failed_run(summary):
                exit_code = 1
            print(
                "[uat] verdict "
                f"zero_leak={summary['zero_leak']} "
                f"rerender={summary['rerender'].get('ok')} "
                f"cleanup_ok={cleanup_ok} "
                f"exit={exit_code}"
            )
            _print_summary(summary)
        return exit_code
    finally:
        restore_settings()
        restore_email()
        if seeded and not keep:
            try:
                with SessionLocal() as session:
                    if any(leftover_counts(session).values()):
                        deleted = cleanup_synthetic_rows(session)
                        session.commit()
                        print("[uat] finally cleanup:", deleted)
            except Exception:
                logger.exception("uat: finally cleanup failed — run --cleanup-only")


if __name__ == "__main__":
    raise SystemExit(main())
