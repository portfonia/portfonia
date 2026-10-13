"""Shared holdings transaction boundary for web and later Agent writers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import HTTPException, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.models.user import User
from app.schemas.holdings import HoldingPatch, ParsedRow
from app.services.accounts import resolve_accounts_for_holdings
from app.services.holding_ordering import sorted_holdings
from app.services.holding_parser import (
    _classify_asset_class,
    apply_confirmed_exchange_suffix,
    normalize_ticker_and_currency,
)
from app.services.instrument_symbols import normalize_legacy_ticker
from app.services.markets import is_capture_supported, resolve_holding_market
from app.services.subscription import is_advanced, is_jade

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


def _canonical(holding: Holding) -> tuple[object, ...]:
    return tuple(getattr(holding, field) for field in _USER_FIELDS)


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


def apply_write_defaults(data: dict[str, Any]) -> dict[str, Any]:
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
    data["asset_class"] = _classify_asset_class(data)
    return data


def _money(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def row_to_holding_data(row: ParsedRow, now: datetime, position: int) -> dict[str, Any]:
    data = row.model_dump(exclude={"issues", "confidence"})
    data = apply_write_defaults(data)
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
    parsed: list[dict[str, Any]] = []
    now = datetime.now(tz=UTC)
    for idx, row in enumerate(rows):
        parsed.append(row_to_holding_data(row, now, start_position + idx))
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
    before = len(existing)
    after = len(rows) if mode == "replace" else before + len(rows)
    introduces = len(rows) > 0
    _ensure_cap(user, before, after, introduces_rows=introduces)
    if mode == "replace":
        changed = before > 0 or after > 0
        if changed:
            session.execute(delete(Holding).where(Holding.user_id == user_id))
            insert_from_rows(
                session,
                user_id,
                rows,
                start_position=0,
                archive_unreferenced=True,
            )
            _bump(user)
        session.commit()
        if changed:
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
        session.commit()
        _enqueue_sparse_for(session, user_id, inserted, log_prefix="confirm_holdings")
    else:
        session.commit()
    return list(
        sorted_holdings(session.scalars(select(Holding).where(Holding.user_id == user_id)).all())
    )


def create_holding(session: Session, user_id: UUID, row: ParsedRow) -> Holding:
    user, existing = lock_user_book(session, user_id)
    _ensure_cap(user, len(existing), len(existing) + 1, introduces_rows=True)
    inserted = insert_from_rows(
        session,
        user_id,
        [row],
        start_position=_next_position(existing),
        archive_unreferenced=False,
    )
    _bump(user)
    session.commit()
    _enqueue_sparse_for(session, user_id, inserted, log_prefix="create_holding")
    holding = inserted[0]
    session.refresh(holding)
    return holding


def reorder_holdings(session: Session, user_id: UUID, ids: list[UUID]) -> list[Holding]:
    user, existing = lock_user_book(session, user_id)
    existing_ids = {row.id for row in existing}
    if len(ids) != len(existing_ids) or set(ids) != existing_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ids must be a permutation of all of this user's holding ids.",
        )
    by_id = {row.id: row for row in existing}
    changed = any(by_id[holding_id].position != idx for idx, holding_id in enumerate(ids))
    for idx, holding_id in enumerate(ids):
        by_id[holding_id].position = idx
    if changed:
        _bump(user)
    session.commit()
    return [by_id[i] for i in ids]


def _holding_dict(holding: Holding) -> dict[str, Any]:
    return {field: getattr(holding, field) for field in _USER_FIELDS if field != "account_id"}


def update_holding(
    session: Session, user_id: UUID, holding_id: UUID, patch: HoldingPatch
) -> Holding:
    updates = patch.model_dump(exclude_unset=True)
    user, existing = lock_user_book(session, user_id)
    holding = next((row for row in existing if row.id == holding_id), None)
    if holding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Holding not found.")
    if not updates:
        return holding
    before = _canonical(holding)
    merged = _holding_dict(holding)
    merged.update(updates)
    try:
        # Validate a copy: ParsedRow coerces amounts to float, while the
        # shared writer must retain the original Decimal values exactly.
        ParsedRow.model_validate(merged.copy())
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    data = apply_write_defaults(merged)
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
    if _canonical(holding) != before:
        _bump(user)
    session.commit()
    if ticker_changed or fund_changed:
        _enqueue_sparse_for(session, user_id, [holding], log_prefix="update_holding")
    session.refresh(holding)
    return holding


def delete_holding(session: Session, user_id: UUID, holding_id: UUID) -> None:
    user, existing = lock_user_book(session, user_id)
    holding = next((row for row in existing if row.id == holding_id), None)
    if holding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Holding not found.")
    session.delete(holding)
    _bump(user)
    session.commit()


def apply_agent_book(
    session: Session,
    user: User,
    existing: list[Holding],
    targets: list[tuple[Holding | None, dict[str, object], int | None]],
    deleted: list[Holding],
    *,
    whole: bool,
) -> tuple[list[Holding], list[Holding], bool]:
    """Apply already checked Agent targets inside the caller's locked transaction.

    Dynamic dictionaries are the shared ORM field mapping, as in the web writer.
    This primitive never commits: the caller saves encrypted replay alongside it.
    """
    after = len(existing) - len(deleted) + sum(row is None for row, _, _ in targets)
    _ensure_cap(
        user, len(existing), after, introduces_rows=any(row is None for row, _, _ in targets)
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
    changed = bool(deleted)
    now = datetime.now(tz=UTC)
    for (holding, data, position), account_id in zip(targets, account_ids, strict=True):
        if holding is None:
            holding = Holding(user_id=user.id, account_id=account_id, position=position, **data)
            if holding.pricing_mode == "manual":
                holding.last_manual_update = now
            session.add(holding)
            changed = True
            capture.append(holding)
        else:
            before = _canonical(holding)
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
            row_changed = before != _canonical(holding)
            if row_changed:
                changed = True
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
    if changed:
        _bump(user)
    session.flush()
    return result, capture, changed


def enqueue_agent_capture(session: Session, user_id: UUID, holdings: list[Holding]) -> None:
    _enqueue_sparse_for(session, user_id, holdings, log_prefix="agent_holdings")
