import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.models.upload_job import UploadJob
from app.schemas.holdings import (
    HoldingOut,
    HoldingPatch,
    ParsedRow,
    ReorderIn,
    UploadJobOut,
)
from app.services import holding_parser
from app.services._yfinance import _normalize_ticker
from app.services.accounts import resolve_accounts_for_holdings
from app.services.holding_parser import (
    _classify_asset_class,
    apply_confirmed_exchange_suffix,
    normalize_ticker_and_currency,
)
from app.services.holdings_export import holdings_export_filename, render_export, render_template
from app.services.markets import is_capture_supported, resolve_holding_market
from app.tasks.holdings_tasks import parse_holdings_upload

logger = logging.getLogger(__name__)

router = APIRouter()


def _sorted_holdings(rows: Sequence[Holding]) -> list[Holding]:
    """Order by ``position`` (issue #92), then name as a stable tiebreaker.

    ``name`` is encrypted (ciphertext at the SQL level), so ``ORDER BY`` at
    the database cannot sort by its real value. ``position`` is plaintext
    and is the user-facing book order (confirm insert, drag-reorder, export).
    TypeDecorator decryption happens transparently on ORM attribute access.
    """
    return sorted(
        rows,
        key=lambda h: (
            h.position is None,
            h.position if h.position is not None else 0,
            h.name,
        ),
    )


_MIN_BARS_FOR_TECHNICAL = 50
# A holdings file is a few dozen to a few thousand rows of text/spreadsheet
# data — this is generous headroom, not a realistic size, meant only to stop
# an oversized upload from being read fully into memory and handed to
# extract/enqueue (PR #82 review). Enforced two ways (PR #82 second review):
# a Content-Length fast-path rejects an obviously oversized request before
# reading any body, and a chunked read aborts once the running total crosses
# the limit rather than materializing the full body first — the earlier
# `content = await file.read()` version read the whole thing into memory
# before ever checking its size.
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024
_UPLOAD_READ_CHUNK_BYTES = 64 * 1024

# _MAX_UPLOAD_BYTES bounds the raw file only. A high text-to-byte-ratio file
# (e.g. an .xlsx/.xls that unpacks into a much larger CSV) can still extract
# to far more text than any real holdings file — same "a few dozen to a few
# thousand rows" headroom logic as _MAX_UPLOAD_BYTES above, applied to the
# extracted text that actually reaches the LLM (issue #54).
_MAX_TEXT_BYTES = 100 * 1024


def _tickers_with_sparse_history(
    session: Session, user_id: UUID, only: set[str] | None = None
) -> list[str]:
    """Return this user's auto-priced tickers with < _MIN_BARS_FOR_TECHNICAL close bars.

    price_snapshots is a global store (no user_id); the *fetch* still writes
    shared rows. The *trigger* is this confirm's book — another user's sparse
    name must not fire a 420-day job here (issue #194).
    """
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
    # price_snapshots is keyed by the normalized ticker (issue #204: e.g.
    # "PSH.L" for a holding whose raw ticker is "PSH") — querying by the raw
    # ticker never matches, so every confirm re-enqueued a fresh 420-day
    # backfill for a ticker that already had a full year of history.
    normalized_to_raw: dict[str, str] = {_normalize_ticker(t): t for t in tickers}
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
        t for t in tickers if bar_counts.get(_normalize_ticker(t), 0) < _MIN_BARS_FOR_TECHNICAL
    )
    if sparse:
        # Concept §8.8: application logs record user_id, never holdings
        # content — a ticker list is holdings-derived (issue #129 §2.2 D).
        logger.info(
            "confirm_holdings: user_id=%s triggered backfill for %d ticker(s) with < %d close bars",
            user_id,
            len(sparse),
            _MIN_BARS_FOR_TECHNICAL,
        )
    return sparse


def _fund_codes_without_close(
    session: Session, user_id: UUID, only: set[str] | None = None
) -> list[str]:
    """Return this user's auto-priced fund_codes with no captured close.

    price_snapshots is global; if any user (or a prior scheduled run) already
    wrote a close under this fund_code, this confirm reuses it. §4.4 technical
    position skips funds (no ticker), so one close is enough for valuation —
    unlike tickers, which backfill when they have < 50 bars (issue #196).
    """
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
        # Concept §8.8: count + user_id, never the fund_code list.
        logger.info(
            "confirm_holdings: user_id=%s triggered fund NAV capture for %d fund(s) with no close snapshot",
            user_id,
            len(missing),
        )
    return missing


def _enqueue_confirm_capture(
    task: Any,
    *args: object,
    label: str,
    user_id: UUID,
    log_prefix: str,
) -> None:
    """Fire-and-forget after holdings are already committed.

    A broker blip must not turn a successful confirm into a 500 — the write
    is done; the daily capture beat covers a missed enqueue. Unlike
    upload_holdings (PR #82), there is no pending job row that would be
    stuck without the task. `log_prefix` is the caller label (confirm /
    create / update) so PATCH/POST do not log as confirm_holdings.
    """
    try:
        task.delay(*args)
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


def _lock_user_holdings(session: Session, user_id: UUID) -> None:
    """Serialize concurrent inserts so max(position) cannot collide.

    Locks this user's holding rows (`FOR UPDATE`). An empty book has nothing
    to lock, so also lock the user row — two first-inserts must not both
    read max=None and land on position 0.
    """
    from app.models.user import User

    session.execute(select(User).where(User.id == user_id).with_for_update())
    session.scalars(select(Holding).where(Holding.user_id == user_id).with_for_update()).all()


def _next_position(session: Session, user_id: UUID) -> int:
    current = session.scalar(select(func.max(Holding.position)).where(Holding.user_id == user_id))
    if current is None:
        return 0
    return int(current) + 1


def _own_holding(session: Session, user_id: UUID, holding_id: UUID) -> Holding:
    holding = session.get(Holding, holding_id)
    if holding is None or holding.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Holding not found.")
    return holding


def _report_locale(session: Session, user_id: UUID) -> str:
    from app.models.user import User

    user = session.get(User, user_id)
    if user is None or user.locale not in ("en", "zh"):
        return "en"
    return user.locale


def _apply_write_defaults(data: dict[str, Any]) -> dict[str, Any]:
    if (
        data.get("asset_type") in ("cash", "wmf")
        and not data.get("ticker")
        and not data.get("fund_code")
    ):
        data["market"] = "Other"
    # Same HK-normalize + suffix-currency correction as the file-import path
    # (holding_parser._postprocess), run before AND after force-suffix for
    # the same reason: before canonicalizes a ticker already suffixed by the
    # caller, after canonicalizes a suffix apply_confirmed_exchange_suffix
    # just added. Without this, an API write of "700"+HKD stored "700.HK"
    # while the identical file-import input stored "0700.HK" — a divergence
    # that silently misses ticker_themes/config-YAML lookups keyed on the
    # canonical form (PR #310 round 5 review).
    normalize_ticker_and_currency(data, emit_note=False)
    apply_confirmed_exchange_suffix(data, emit_note=False)
    normalize_ticker_and_currency(data, emit_note=False)
    # Recompute capture support server-side so a client cannot enable
    # speculative yfinance by forging capture_supported=True (issue #311).
    # Suffix first so PSH -> PSH.L resolves as UK, not as a bare-US ticker.
    resolved_market, capture_ok = resolve_holding_market(
        ticker=data.get("ticker"),
        declared_market=data.get("market"),
        fund_code=data.get("fund_code"),
        asset_type=data.get("asset_type"),
        pricing_mode=data.get("pricing_mode") or "auto",
    )
    data["market"] = resolved_market
    # Mirrors _postprocess: a row apply_confirmed_exchange_suffix left
    # ambiguously-suffixed is still bare as far as resolve_holding_market's
    # ticker-based inference is concerned, so its "no suffix = US" default
    # would otherwise win regardless of the real (persisted) market — never
    # capture-ready without a real suffix (PR #310 round 6 review).
    if data.pop("_suffix_ambiguous", False):
        capture_ok = False
    data["capture_supported"] = capture_ok
    data["asset_class"] = _classify_asset_class(data)
    return data


def _row_to_holding_data(row: ParsedRow, now: datetime, position: int) -> dict[str, Any]:
    data = row.model_dump(exclude={"issues", "confidence"})
    data = _apply_write_defaults(data)
    for field in ("shares", "avg_cost", "current_value"):
        if data[field] is not None:
            data[field] = Decimal(str(data[field]))
    if data["pricing_mode"] == "manual":
        data["last_manual_update"] = now
    data["position"] = position
    return data


def _insert_from_rows(
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
        parsed.append(_row_to_holding_data(row, now, start_position + idx))
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
        sparse = _tickers_with_sparse_history(session, user_id, only=tickers)
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
        missing = _fund_codes_without_close(session, user_id, only=codes)
        if missing:
            from app.tasks.capture_tasks import backfill_fund_navs_task

            _enqueue_confirm_capture(
                backfill_fund_navs_task,
                missing,
                label="fund NAV capture",
                user_id=user_id,
                log_prefix=log_prefix,
            )


def _enqueue_sector_backfill(
    user_id: UUID, holdings: Sequence[Holding], *, log_prefix: str
) -> None:
    """Fire-and-forget sector fill so POST/PATCH/confirm do not wait on yfinance.

    The task opens its own session and commits — a request-scoped
    sector flush after session.commit() is rolled back when
    get_session() closes (PR #310).
    """
    ids = [str(h.id) for h in holdings]
    if not ids:
        return
    from app.tasks.capture_tasks import backfill_sectors_task

    _enqueue_confirm_capture(
        backfill_sectors_task,
        ids,
        str(user_id),
        label="sector backfill",
        user_id=user_id,
        log_prefix=log_prefix,
    )


_MONEY_FIELDS = ("shares", "avg_cost", "current_value")


def _holding_as_row_dict(holding: Holding) -> dict[str, Any]:
    return {
        "name": holding.name,
        "ticker": holding.ticker,
        "fund_code": holding.fund_code,
        "currency": holding.currency,
        "shares": float(holding.shares) if holding.shares is not None else None,
        "avg_cost": float(holding.avg_cost) if holding.avg_cost is not None else None,
        "current_value": (
            float(holding.current_value) if holding.current_value is not None else None
        ),
        "pricing_mode": holding.pricing_mode,
        "asset_type": holding.asset_type,
        "asset_class": holding.asset_class,
        "market": holding.market,
        "broker": holding.broker,
        "account": holding.account,
        "portfolio": holding.portfolio,
        "notes": holding.notes,
    }


@router.post("/upload", response_model=UploadJobOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_holdings(
    request: Request,
    file: UploadFile,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> UploadJob:
    """Kick off an async parse of the uploaded file (issue #77).

    Returns immediately with a pending job; the client polls
    GET /holdings/upload/{job_id} for the result. The previous synchronous
    version held one HTTP connection open for the full parse (holding_parser
    .parse()'s internal retry loop — issue #78), observed taking ~5min in
    one case: fragile against any interruption on that connection in the
    meantime — the backend finished and returned 200, but the client never
    saw it because the connection had already dropped.
    """
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No filename provided."
        )
    # Fast path: reject an obviously oversized request before reading any
    # body. This reflects the whole multipart body (boundary + other fields
    # too), not just this file, so it's a coarse pre-check — the chunked
    # read below is the real bound.
    content_length = request.headers.get("content-length")
    if (
        content_length is not None
        and content_length.isdigit()
        and int(content_length) > _MAX_UPLOAD_BYTES
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"File too large ({content_length} bytes) — max {_MAX_UPLOAD_BYTES} bytes.",
        )
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_UPLOAD_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"File too large — max {_MAX_UPLOAD_BYTES} bytes.",
            )
        chunks.append(chunk)
    content = b"".join(chunks)
    try:
        text = holding_parser._extract_text(content, file.filename)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Extracted text too large — max {_MAX_TEXT_BYTES} bytes.",
        )

    # raw_text is cleared by the Celery task once the parse attempt finishes
    # (success or failure) — the task takes job_id only, never the text
    # itself, so a holdings file's content never becomes a Celery/Redis
    # broker message argument (PR #82 review).
    job = UploadJob(
        user_id=principal.user_id, filename=file.filename, status="pending", raw_text=text
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    try:
        parse_holdings_upload.delay(str(job.id))
    except Exception as exc:
        # Enqueue failed after the row was already committed — without this,
        # the job would sit at status="pending" forever with no task ever
        # attached to pick it up (PR #82 review).
        logger.exception("upload_holdings: failed to enqueue job %s", job.id)
        job.status = "failed"
        job.error = f"Failed to queue parse job: {type(exc).__name__}: {exc}"
        job.raw_text = None
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not queue the upload for processing. Please try again.",
        ) from exc
    return job


@router.get("/upload/{job_id}", response_model=UploadJobOut)
def get_upload_job(
    job_id: UUID,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> UploadJob:
    job = session.get(UploadJob, job_id)
    if job is None or job.user_id != principal.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload job not found.")
    return job


@router.post("/confirm", response_model=list[HoldingOut])
def confirm_holdings(
    rows: list[ParsedRow],
    mode: Literal["append", "replace"] = Query("append"),
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[Holding]:
    """Persist parsed rows. Default mode is append (issue #92) — safer than
    silently replacing the whole book. Frontend always sends mode explicitly.
    """
    user_id = principal.user_id
    if mode == "replace":
        session.execute(delete(Holding).where(Holding.user_id == user_id))
        start = 0
        archive = True
    else:
        _lock_user_holdings(session, user_id)
        start = _next_position(session, user_id)
        archive = False
    inserted = _insert_from_rows(
        session,
        user_id,
        rows,
        start_position=start,
        archive_unreferenced=archive,
    )
    session.commit()
    _enqueue_sector_backfill(user_id, inserted, log_prefix="confirm_holdings")
    if mode == "replace":
        tickers_needing_backfill = _tickers_with_sparse_history(session, user_id)
        if tickers_needing_backfill:
            from app.tasks.capture_tasks import backfill_ohlcv_task

            _enqueue_confirm_capture(
                backfill_ohlcv_task,
                tickers_needing_backfill,
                label="ohlcv backfill",
                user_id=user_id,
                log_prefix="confirm_holdings",
            )
        fund_codes_needing_nav = _fund_codes_without_close(session, user_id)
        if fund_codes_needing_nav:
            from app.tasks.capture_tasks import backfill_fund_navs_task

            _enqueue_confirm_capture(
                backfill_fund_navs_task,
                fund_codes_needing_nav,
                label="fund NAV capture",
                user_id=user_id,
                log_prefix="confirm_holdings",
            )
    else:
        _enqueue_sparse_for(session, user_id, inserted, log_prefix="confirm_holdings")
    return _sorted_holdings(
        session.scalars(select(Holding).where(Holding.user_id == user_id)).all()
    )


@router.get("", response_model=list[HoldingOut])
def list_holdings(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[Holding]:
    rows = _sorted_holdings(
        session.scalars(select(Holding).where(Holding.user_id == principal.user_id)).all()
    )
    return list(rows)


@router.post("", response_model=HoldingOut, status_code=status.HTTP_201_CREATED)
def create_holding(
    row: ParsedRow,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Holding:
    """Create one holding from a structured body. Does not call the LLM."""
    user_id = principal.user_id
    _lock_user_holdings(session, user_id)
    inserted = _insert_from_rows(
        session,
        user_id,
        [row],
        start_position=_next_position(session, user_id),
        archive_unreferenced=False,
    )
    session.commit()
    _enqueue_sector_backfill(user_id, inserted, log_prefix="create_holding")
    _enqueue_sparse_for(session, user_id, inserted, log_prefix="create_holding")
    holding = inserted[0]
    session.refresh(holding)
    return holding


@router.patch("/reorder", response_model=list[HoldingOut])
def reorder_holdings(
    body: ReorderIn,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[Holding]:
    user_id = principal.user_id
    _lock_user_holdings(session, user_id)
    existing = list(session.scalars(select(Holding).where(Holding.user_id == user_id)).all())
    existing_ids = {h.id for h in existing}
    incoming = body.ids
    if len(incoming) != len(existing_ids) or set(incoming) != existing_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ids must be a permutation of all of this user's holding ids.",
        )
    by_id = {h.id: h for h in existing}
    for idx, holding_id in enumerate(incoming):
        by_id[holding_id].position = idx
    session.commit()
    return [by_id[i] for i in incoming]


@router.get("/export")
def export_holdings(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Response:
    rows = _sorted_holdings(
        session.scalars(select(Holding).where(Holding.user_id == principal.user_id)).all()
    )
    md = render_export(list(rows), _report_locale(session, principal.user_id))
    filename = holdings_export_filename()
    return Response(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/template")
def holdings_template(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Response:
    md = render_template(_report_locale(session, principal.user_id))
    return Response(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": "attachment; filename=holdings-template.md"},
    )


@router.patch("/{holding_id}", response_model=HoldingOut)
def update_holding(
    holding_id: UUID,
    patch: HoldingPatch,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Holding:
    holding = _own_holding(session, principal.user_id, holding_id)
    updates = patch.model_dump(exclude_unset=True)
    if not updates:
        return holding
    merged = _holding_as_row_dict(holding)
    merged.update(updates)
    # Re-run ParsedRow validation (cash/wmf boundary, currency, asset_class)
    # on the merged state so a partial patch cannot leave an illegal row.
    parsed = ParsedRow.model_validate(merged)
    data = _apply_write_defaults(parsed.model_dump(exclude={"issues", "confidence"}))
    account_fields_changed = any(k in updates for k in ("broker", "account", "portfolio"))
    # Compare against what _apply_write_defaults actually produced, not just
    # whether the client's PATCH body mentioned "ticker": it force-suffixes
    # a ticker (apply_confirmed_exchange_suffix) even on a patch that never
    # touches the field, e.g. a notes-only edit on a legacy unsuffixed row.
    # Comparing `updates["ticker"]` missed that case entirely — stale
    # sector/price survived and no backfill was enqueued, the round-1
    # regression reopened through a different door (PR #310 round 5 review).
    ticker_changed = data.get("ticker") != holding.ticker
    fund_changed = data.get("fund_code") != holding.fund_code
    for field, value in data.items():
        if field in _MONEY_FIELDS:
            # Untouched EncryptedDecimal columns must not round-trip through
            # float (PR #310). Only rewrite money fields actually present
            # in HoldingPatch.
            if field not in updates:
                continue
            if value is not None:
                value = Decimal(str(value))
        setattr(holding, field, value)
    if data["pricing_mode"] == "manual":
        holding.last_manual_update = datetime.now(tz=UTC)
    if ticker_changed or fund_changed:
        holding.sector = None
        holding.market_price = None
        holding.price_as_of = None
        holding.price_fetched_at = None
    if account_fields_changed:
        account_ids = resolve_accounts_for_holdings(
            session,
            principal.user_id,
            [(holding.broker, holding.account, holding.portfolio)],
            archive_unreferenced=False,
        )
        holding.account_id = account_ids[0]
    session.commit()
    if ticker_changed or fund_changed:
        _enqueue_sector_backfill(principal.user_id, [holding], log_prefix="update_holding")
        _enqueue_sparse_for(session, principal.user_id, [holding], log_prefix="update_holding")
    session.refresh(holding)
    return holding


@router.delete("/{holding_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_holding(
    holding_id: UUID,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Response:
    holding = _own_holding(session, principal.user_id, holding_id)
    session.delete(holding)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
