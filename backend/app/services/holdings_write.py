"""Shared holdings transaction boundary for web and later Agent writers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import HTTPException, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core import alert_dedup
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.models.user import User
from app.schemas.holdings import HoldingPatch, ParsedRow
from app.services import holding_changes
from app.services.accounts import resolve_accounts_for_holdings
from app.services.holding_change_alerts import enqueue_change_alert
from app.services.holding_ordering import sorted_holdings
from app.services.holding_parser import (
    _classify_asset_class,
    _load_ticker_asset_class,
    apply_confirmed_exchange_suffix,
    normalize_ticker_and_currency,
    unmapped_fund_identifier,
)
from app.services.instrument_symbols import normalize_legacy_ticker
from app.services.markets import is_capture_supported, resolve_holding_market
from app.services.subscription import is_advanced, is_jade
from app.services.ticker_leverage import load_leverage_entries, normalize_leverage_ticker

logger = logging.getLogger(__name__)

_MIN_BARS_FOR_TECHNICAL = 50
_MONEY_FIELDS = ("shares", "avg_cost", "current_value")
_USER_FIELDS = (
    "name",
    "ticker",
    "fund_code",
    "currency",
    "shares",
    "avg_cost",
    "current_value",
    "pricing_mode",
    "asset_type",
    "asset_class",
    "market",
    "capture_supported",
    "broker",
    "account",
    "portfolio",
    "notes",
    "watch_tier",
    "position",
    "account_id",
)


def enqueue_unmapped_asset_class_alert(
    holdings: Iterable[Holding],
    source: Literal["file", "web", "rollback", "agent"],
    session: Session,
) -> None:
    """Alert ops once per identifier (7 days) when a fund/ETF used the catch-all class."""
    try:
        classes = {
            t for t, e in load_leverage_entries(session).items() if e.asset_class is not None
        }
        found: dict[str, Holding] = {}
        for h in holdings:
            if h.ticker and normalize_leverage_ticker(h.ticker) in classes:
                continue
            identifier = unmapped_fund_identifier(
                {"asset_type": h.asset_type, "ticker": h.ticker, "fund_code": h.fund_code}
            )
            if identifier is not None and identifier not in found:
                found[identifier] = h
        fresh = {
            i: h for i, h in found.items() if not alert_dedup.already_alerted(_unmapped_key(i))
        }
        if not fresh:
            return
        from app.tasks.admin_tasks import send_admin_alert_task

        for identifier in fresh:
            alert_dedup.mark_alerted(_unmapped_key(identifier), 7 * 24 * 3600)
        lines = [
            f"{identifier} market={h.market} asset_type={h.asset_type} stored as EQUITY_BROAD"
            for identifier, h in fresh.items()
        ]
        body = "\n".join(
            [
                *lines,
                "",
                f"Write source: {source}",
                "",
                "Add the identifier to backend/config/ticker_asset_class.yml by PR, deploy, "
                "then run `python -m app.scripts.reclassify_asset_class` (dry run) and, with "
                "the owner's approval, `--apply`.",
            ]
        )
        send_admin_alert_task.delay(
            f"Unmapped fund/ETF asset_class ({len(fresh)} instrument(s))",
            body,
            severity="WARNING",
        )
    except Exception:
        logger.error("Unmapped asset_class alert failed source=%s", source)


def _unmapped_key(identifier: str) -> str:
    return f"unmapped_asset_class:{identifier}"


def holding_cap(user: User) -> int:
    if is_jade(user):
        return 100
    if is_advanced(user):
        return 50
    return 30


def format_holdings_version(version: int) -> str:
    return str(version)


def lock_user_book(session: Session, user_id: UUID) -> tuple[User, list[Holding]]:
    user = session.execute(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    rows = list(
        session.scalars(select(Holding).where(Holding.user_id == user_id).with_for_update()).all()
    )
    return user, rows


def _ensure_cap(user: User, before: int, after: int, *, introduces_rows: bool) -> None:
    cap = holding_cap(user)
    allowed = after <= cap if before <= cap or introduces_rows else after <= before
    if not allowed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="holding_cap")


def _bump(user: User) -> None:
    user.holdings_version = int(user.holdings_version) + 1


def tickers_with_sparse_history(
    session: Session, user_id: UUID, only: set[str] | None = None
) -> list[str]:
    holdings = session.scalars(
        select(Holding).where(
            Holding.user_id == user_id,
            Holding.ticker.is_not(None),
            Holding.pricing_mode == "auto",
        )
    ).all()
    tickers = {h.ticker for h in holdings if h.ticker and is_capture_supported(h)}
    if only is not None:
        tickers &= only
    if not tickers:
        return []
    normalized_to_raw: dict[str, str] = {normalize_legacy_ticker(t): t for t in tickers}
    bar_counts: dict[str, int] = {
        row[0]: row[1]
        for row in session.execute(
            select(PriceSnapshot.ticker, func.count().label("bars"))
            .where(
                PriceSnapshot.ticker.in_(normalized_to_raw),
                PriceSnapshot.session_node == "close",
                PriceSnapshot.close.is_not(None),
            )
            .group_by(PriceSnapshot.ticker)
        ).all()
    }
    sparse = sorted(
        t
        for t in tickers
        if bar_counts.get(normalize_legacy_ticker(t), 0) < _MIN_BARS_FOR_TECHNICAL
    )
    if sparse:
        logger.info(
            "confirm_holdings: user_id=%s triggered backfill for %d ticker(s) with < %d close bars",
            user_id,
            len(sparse),
            _MIN_BARS_FOR_TECHNICAL,
        )
    return sparse


def fund_codes_without_close(
    session: Session, user_id: UUID, only: set[str] | None = None
) -> list[str]:
    holdings = session.scalars(
        select(Holding).where(
            Holding.user_id == user_id,
            Holding.fund_code.is_not(None),
            Holding.pricing_mode == "auto",
        )
    ).all()
    codes = {h.fund_code for h in holdings if h.fund_code}
    if only is not None:
        codes &= only
    if not codes:
        return []
    cached = set(
        session.scalars(
            select(PriceSnapshot.ticker)
            .where(
                PriceSnapshot.ticker.in_(codes),
                PriceSnapshot.session_node == "close",
                PriceSnapshot.close.is_not(None),
            )
            .distinct()
        ).all()
    )
    missing = sorted(c for c in codes if c not in cached)
    if missing:
        logger.info(
            "confirm_holdings: user_id=%s triggered fund NAV capture for %d fund(s) with no close snapshot",
            user_id,
            len(missing),
        )
    return missing


def _enqueue_confirm_capture(
    task: Callable[..., object],
    *args: object,
    label: str,
    user_id: UUID,
    log_prefix: str,
) -> None:
    try:
        task.delay(*args)  # type: ignore[attr-defined]
    except Exception:
        first = args[0] if args else []
        count = len(first) if isinstance(first, list) else 1
        logger.exception(
            "%s: user_id=%s failed to enqueue %s for %d identifier(s)",
            log_prefix,
            user_id,
            label,
            count,
        )


def apply_write_defaults(
    data: dict[str, Any], leverage_classes: Mapping[str, str] | None = None
) -> dict[str, Any]:
    if (
        data.get("asset_type") in ("cash", "wmf")
        and not data.get("ticker")
        and not data.get("fund_code")
    ):
        data["market"] = "Other"
    normalize_ticker_and_currency(data, emit_note=False)
    apply_confirmed_exchange_suffix(data, emit_note=False)
    normalize_ticker_and_currency(data, emit_note=False)
    resolved_market, capture_ok = resolve_holding_market(
        ticker=data.get("ticker"),
        declared_market=data.get("market"),
        fund_code=data.get("fund_code"),
        asset_type=data.get("asset_type"),
        pricing_mode=data.get("pricing_mode") or "auto",
    )
    data["market"] = resolved_market
    if data.pop("_suffix_ambiguous", False):
        capture_ok = False
    data["capture_supported"] = capture_ok
    data["asset_class"] = _classify_asset_class(data, leverage_classes)
    return data


def _money(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def row_to_holding_data(
    row: ParsedRow, now: datetime, position: int, leverage_classes: Mapping[str, str]
) -> dict[str, Any]:
    data = row.model_dump(exclude={"issues", "confidence"})
    data = apply_write_defaults(data, leverage_classes)
    for field in _MONEY_FIELDS:
        if data[field] is not None:
            data[field] = _money(data[field])
    if data["pricing_mode"] == "manual":
        data["last_manual_update"] = now
    data["position"] = position
    return data


def insert_from_rows(
    session: Session,
    user_id: UUID,
    rows: list[ParsedRow],
    *,
    start_position: int,
    archive_unreferenced: bool,
) -> list[Holding]:
    leverage_classes = {
        t: e.asset_class
        for t, e in load_leverage_entries(session).items()
        if e.asset_class is not None
    }
    parsed: list[dict[str, Any]] = []
    now = datetime.now(tz=UTC)
    for idx, row in enumerate(rows):
        parsed.append(row_to_holding_data(row, now, start_position + idx, leverage_classes))
    account_ids = resolve_accounts_for_holdings(
        session,
        user_id,
        [(d["broker"], d["account"], d["portfolio"]) for d in parsed],
        archive_unreferenced=archive_unreferenced,
    )
    holdings = [
        Holding(user_id=user_id, account_id=account_id, **data)
        for data, account_id in zip(parsed, account_ids, strict=True)
    ]
    session.add_all(holdings)
    return holdings


def _enqueue_sparse_for(
    session: Session,
    user_id: UUID,
    holdings: Sequence[Holding],
    *,
    log_prefix: str,
) -> None:
    tickers = {h.ticker for h in holdings if h.ticker and h.pricing_mode == "auto"}
    if tickers:
        sparse = tickers_with_sparse_history(session, user_id, only=tickers)
        if sparse:
            from app.tasks.capture_tasks import backfill_ohlcv_task

            _enqueue_confirm_capture(
                backfill_ohlcv_task,
                sparse,
                label="ohlcv backfill",
                user_id=user_id,
                log_prefix=log_prefix,
            )
    codes = {h.fund_code for h in holdings if h.fund_code and h.pricing_mode == "auto"}
    if codes:
        missing = fund_codes_without_close(session, user_id, only=codes)
        if missing:
            from app.tasks.capture_tasks import backfill_fund_navs_task

            _enqueue_confirm_capture(
                backfill_fund_navs_task,
                missing,
                label="fund NAV capture",
                user_id=user_id,
                log_prefix=log_prefix,
            )


def _enqueue_replaced_book(session: Session, user_id: UUID) -> None:
    tickers_needing_backfill = tickers_with_sparse_history(session, user_id)
    if tickers_needing_backfill:
        from app.tasks.capture_tasks import backfill_ohlcv_task

        _enqueue_confirm_capture(
            backfill_ohlcv_task,
            tickers_needing_backfill,
            label="ohlcv backfill",
            user_id=user_id,
            log_prefix="confirm_holdings",
        )
    fund_codes_needing_nav = fund_codes_without_close(session, user_id)
    if fund_codes_needing_nav:
        from app.tasks.capture_tasks import backfill_fund_navs_task

        _enqueue_confirm_capture(
            backfill_fund_navs_task,
            fund_codes_needing_nav,
            label="fund NAV capture",
            user_id=user_id,
            log_prefix="confirm_holdings",
        )


def _next_position(rows: Sequence[Holding]) -> int:
    positions = [row.position for row in rows if row.position is not None]
    if not positions:
        return 0
    return max(positions) + 1


def confirm_holdings(
    session: Session,
    user_id: UUID,
    rows: list[ParsedRow],
    mode: Literal["append", "replace"],
) -> list[Holding]:
    rows = [row.model_copy(update={"watch_tier": None}) for row in rows]
    user, existing = lock_user_book(session, user_id)
    change_before = holding_changes.snapshot(existing)
    before = len(existing)
    after = len(rows) if mode == "replace" else before + len(rows)
    introduces = len(rows) > 0
    _ensure_cap(user, before, after, introduces_rows=introduces)
    group_id = None
    if mode == "replace":
        changed = before > 0 or after > 0
        if changed:
            session.execute(delete(Holding).where(Holding.user_id == user_id))
            replaced = insert_from_rows(
                session,
                user_id,
                rows,
                start_position=0,
                archive_unreferenced=True,
            )
            _bump(user)
            group_id = holding_changes.record(session, user_id, change_before, source="file")
        session.commit()
        if group_id is not None:
            enqueue_change_alert(user_id, group_id)
        if changed:
            enqueue_unmapped_asset_class_alert(replaced, "file", session)
            enqueue_leverage_check(session, replaced)
            _enqueue_replaced_book(session, user_id)
    elif rows:
        inserted = insert_from_rows(
            session,
            user_id,
            rows,
            start_position=_next_position(existing),
            archive_unreferenced=False,
        )
        _bump(user)
        group_id = holding_changes.record(session, user_id, change_before, source="file")
        session.commit()
        if group_id is not None:
            enqueue_change_alert(user_id, group_id)
        enqueue_unmapped_asset_class_alert(inserted, "file", session)
        enqueue_leverage_check(session, inserted)
        _enqueue_sparse_for(session, user_id, inserted, log_prefix="confirm_holdings")
    else:
        session.commit()
    return list(
        sorted_holdings(session.scalars(select(Holding).where(Holding.user_id == user_id)).all())
    )


def create_holding(session: Session, user_id: UUID, row: ParsedRow) -> Holding:
    user, existing = lock_user_book(session, user_id)
    change_before = holding_changes.snapshot(existing)
    _ensure_cap(user, len(existing), len(existing) + 1, introduces_rows=True)
    inserted = insert_from_rows(
        session,
        user_id,
        [row],
        start_position=_next_position(existing),
        archive_unreferenced=False,
    )
    _bump(user)
    holding_changes.record(session, user_id, change_before, source="web")
    session.commit()
    enqueue_unmapped_asset_class_alert(inserted, "web", session)
    enqueue_leverage_check(session, inserted)
    _enqueue_sparse_for(session, user_id, inserted, log_prefix="create_holding")
    holding = inserted[0]
    session.refresh(holding)
    return holding


def reorder_holdings(session: Session, user_id: UUID, ids: list[UUID]) -> list[Holding]:
    user, existing = lock_user_book(session, user_id)
    change_before = holding_changes.snapshot(existing)
    existing_ids = {row.id for row in existing}
    if len(ids) != len(existing_ids) or set(ids) != existing_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ids must be a permutation of all of this user's holding ids.",
        )
    by_id = {row.id: row for row in existing}
    for idx, holding_id in enumerate(ids):
        by_id[holding_id].position = idx
    _bump(user)
    holding_changes.record(session, user_id, change_before, source="web")
    session.commit()
    return [by_id[i] for i in ids]


def _holding_dict(holding: Holding) -> dict[str, Any]:
    return {field: getattr(holding, field) for field in _USER_FIELDS if field != "account_id"}


def update_holding(
    session: Session, user_id: UUID, holding_id: UUID, patch: HoldingPatch
) -> Holding:
    updates = patch.model_dump(exclude_unset=True)
    user, existing = lock_user_book(session, user_id)
    change_before = holding_changes.snapshot(existing)
    holding = next((row for row in existing if row.id == holding_id), None)
    if holding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Holding not found.")
    if not updates:
        _bump(user)
        session.commit()
        return holding
    merged = _holding_dict(holding)
    merged.update(updates)
    try:
        # Validate a copy: ParsedRow coerces amounts to float, while the
        # shared writer must retain the original Decimal values exactly.
        ParsedRow.model_validate(merged.copy())
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    leverage_classes = {
        t: e.asset_class
        for t, e in load_leverage_entries(session).items()
        if e.asset_class is not None
    }
    data = apply_write_defaults(merged, leverage_classes)
    account_fields_changed = any(key in updates for key in ("broker", "account", "portfolio"))
    ticker_changed = data.get("ticker") != holding.ticker
    fund_changed = data.get("fund_code") != holding.fund_code
    for field, value in data.items():
        if field in _MONEY_FIELDS:
            if field not in updates:
                continue
            if value is not None:
                value = _money(value)
        if field in ("position", "account_id"):
            continue
        setattr(holding, field, value)
    if data["pricing_mode"] == "manual":
        holding.last_manual_update = datetime.now(tz=UTC)
    if ticker_changed or fund_changed:
        holding.market_price = None
        holding.price_as_of = None
        holding.price_fetched_at = None
    if account_fields_changed:
        account_ids = resolve_accounts_for_holdings(
            session,
            user_id,
            [(holding.broker, holding.account, holding.portfolio)],
            archive_unreferenced=False,
        )
        holding.account_id = account_ids[0]
    _bump(user)
    holding_changes.record(session, user_id, change_before, source="web")
    session.commit()
    enqueue_unmapped_asset_class_alert([holding], "web", session)
    enqueue_leverage_check(session, [holding])
    if ticker_changed or fund_changed:
        _enqueue_sparse_for(session, user_id, [holding], log_prefix="update_holding")
    session.refresh(holding)
    return holding


def delete_holding(session: Session, user_id: UUID, holding_id: UUID) -> None:
    user, existing = lock_user_book(session, user_id)
    change_before = holding_changes.snapshot(existing)
    holding = next((row for row in existing if row.id == holding_id), None)
    if holding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Holding not found.")
    session.delete(holding)
    _bump(user)
    holding_changes.record(session, user_id, change_before, source="web")
    session.commit()


def apply_agent_book(
    session: Session,
    user: User,
    targets: list[tuple[Holding | None, dict[str, object], int | None]],
    deleted: list[Holding],
    *,
    whole: bool,
    token_id: UUID,
) -> tuple[list[Holding], list[Holding], bool, UUID | None]:
    """Apply already checked Agent targets inside the caller's locked transaction.

    The caller must first run agent_holdings.plan() under the same user lock;
    that plan owns the final cap check for these targets and deletions.
    Dynamic dictionaries are the shared ORM field mapping, as in the web writer.
    This primitive never commits: the caller saves encrypted replay alongside it.
    """
    change_before = holding_changes.snapshot(
        session.scalars(select(Holding).where(Holding.user_id == user.id)).all()
    )
    account_ids = resolve_accounts_for_holdings(
        session,
        user.id,
        [
            (
                cast(str | None, data.get("broker")),
                cast(str | None, data.get("account")),
                cast(str | None, data.get("portfolio")),
            )
            for _, data, _ in targets
        ],
        archive_unreferenced=whole,
    )
    result: list[Holding] = []
    capture: list[Holding] = []
    now = datetime.now(tz=UTC)
    for (holding, data, position), account_id in zip(targets, account_ids, strict=True):
        if holding is None:
            holding = Holding(user_id=user.id, account_id=account_id, position=position, **data)
            if holding.pricing_mode == "manual":
                holding.last_manual_update = now
            session.add(holding)
            capture.append(holding)
        else:
            identity_changed = holding.ticker != data.get(
                "ticker"
            ) or holding.fund_code != data.get("fund_code")
            manual_value_changed = (
                holding.current_value != data.get("current_value")
                or holding.pricing_mode != data["pricing_mode"]
            )
            for field, value in data.items():
                setattr(holding, field, value)
            holding.account_id = account_id
            holding.position = position
            if holding.pricing_mode == "manual" and manual_value_changed:
                holding.last_manual_update = now
            if identity_changed:
                holding.market_price = None
                holding.price_as_of = None
                holding.price_fetched_at = None
                capture.append(holding)
        result.append(holding)
    for holding in deleted:
        session.delete(holding)
    _bump(user)
    session.flush()
    group_id = holding_changes.record(
        session, user.id, change_before, source="agent", token_id=token_id
    )
    return result, capture, True, group_id


def enqueue_agent_capture(session: Session, user_id: UUID, holdings: list[Holding]) -> None:
    _enqueue_sparse_for(session, user_id, holdings, log_prefix="agent_holdings")


def rollback_holdings(session: Session, user_id: UUID, group_id: UUID, version: str) -> None:
    user, rows = lock_user_book(session, user_id)
    groups = holding_changes.retained_groups(session, user_id)
    if not any(group.id == group_id for group in groups):
        raise HTTPException(404, "Holding change not found.")
    if version != format_holdings_version(user.holdings_version):
        raise HTTPException(409, "version_conflict")
    before = holding_changes.snapshot(rows)
    group, target, _ = holding_changes.rollback_target(before, groups, group_id)
    if target == before:
        return
    restored = target.keys() - before.keys()
    _ensure_cap(user, len(before), len(target), introduces_rows=bool(restored))
    by_id = {row.id: row for row in rows}
    for hid in before.keys() - target.keys():
        session.delete(by_id[hid])
    written = {hid: data for hid, data in target.items() if before.get(hid) != data}
    account_ids = resolve_accounts_for_holdings(
        session,
        user_id,
        [
            (
                cast(str | None, data["broker"]),
                cast(str | None, data["account"]),
                cast(str | None, data["portfolio"]),
            )
            for data in written.values()
        ],
        archive_unreferenced=False,
    )
    capture: list[Holding] = []
    written_rows: list[Holding] = []
    now = datetime.now(tz=UTC)
    leverage_classes = {
        t: e.asset_class
        for t, e in load_leverage_entries(session).items()
        if e.asset_class is not None
    }
    for (hid, values), account_id in zip(written.items(), account_ids, strict=True):
        data = {
            field: _money(value) if field in _MONEY_FIELDS else value
            for field, value in values.items()
        }
        defaults = apply_write_defaults(data.copy(), leverage_classes)
        data.update(
            asset_class=defaults["asset_class"], capture_supported=defaults["capture_supported"]
        )
        row = by_id.get(hid)
        identity_changed = (
            row is None or row.ticker != data["ticker"] or row.fund_code != data["fund_code"]
        )
        manual_changed = (
            row is None
            or row.current_value != data["current_value"]
            or row.pricing_mode != data["pricing_mode"]
        )
        if row is None:
            row = Holding(id=hid, user_id=user_id, **data)
            session.add(row)
        else:
            for field, value in data.items():
                setattr(row, field, value)
            if identity_changed:
                row.market_price = None
                row.price_as_of = None
                row.price_fetched_at = None
        row.account_id = account_id
        written_rows.append(row)
        if row.pricing_mode == "manual" and manual_changed:
            row.last_manual_update = now
        if identity_changed:
            capture.append(row)
    _bump(user)
    holding_changes.record(
        session, user_id, before, source="rollback", rollback_to=group.created_at
    )
    session.commit()
    enqueue_unmapped_asset_class_alert(written_rows, "rollback", session)
    enqueue_leverage_check(session, written_rows)
    _enqueue_sparse_for(session, user_id, capture, log_prefix="rollback_holdings")


def leverage_check_candidates(
    session: Session, holdings: Iterable[Holding], *, ignore_redis: bool = False
) -> list[str]:
    entries = load_leverage_entries(session)
    mapping = _load_ticker_asset_class()
    candidates = set()
    for h in holdings:
        if h.ticker is None or h.pricing_mode != "auto" or not h.capture_supported:
            continue
        ticker = normalize_leverage_ticker(h.ticker)
        if ticker in entries or h.ticker.upper() in mapping:
            continue
        if not ignore_redis and alert_dedup.already_alerted(f"leverage_check:{ticker}"):
            continue
        candidates.add(ticker)
    return sorted(candidates)


def enqueue_leverage_check(session: Session, holdings: Iterable[Holding]) -> None:
    try:
        candidates = leverage_check_candidates(session, holdings)
        if candidates:
            from app.tasks.admin_tasks import check_leveraged_tickers_task

            check_leveraged_tickers_task.delay(candidates)
    except Exception:
        logger.error("Leverage check enqueue failed", exc_info=True)
