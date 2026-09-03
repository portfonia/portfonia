"""One-off backfill: resolve `capture_supported`/`market`/`ticker` for
pre-existing `market="Other"` holdings (issue #313 item 4).

Migration `c7d8e9f0a1b2` (issue #311) added `capture_supported` defaulting
True for every existing row, including ones already stored as
`market="Other"` — the migration runs raw SQL against Fernet-encrypted
ciphertext and cannot decrypt `ticker` to re-resolve it. This script runs
the exact same suffix-forcing + resolution sequence
`_apply_write_defaults` (routers/holdings.py) already applies to every
fresh single-row write/confirm — `normalize_ticker_and_currency` ->
`apply_confirmed_exchange_suffix` -> `normalize_ticker_and_currency` ->
`resolve_holding_market`, with the same `_suffix_ambiguous` override —
scoped to existing `market="Other"` rows only. Confirm-parity matters: a
still-bare ticker (e.g. "VOD", no exchange suffix) with a currency hint
(e.g. GBP) gets the suffix forced BEFORE market resolution runs, so it
reclassifies as UK/`VOD.L`, not a bare-ticker "no suffix = US" promotion
(the #204 collision class this whole capture series exists to kill — PR
#314 review round 1 caught an earlier version of this script that called
`resolve_holding_market` directly, skipping the suffix-forcing step, and
would have silently promoted every currency-hinted bare-Other ticker to
US/capture_supported=True). A ticker with no market-determining signal at
all (no suffix, no currency hint, no fund_code, no override-table hit)
still falls through to `resolve_holding_market`'s own bare-ticker
default (US) — same as every other write path in this app; there is no
data available here to disambiguate it further.

Deliberately NOT a migration — the issue is explicit that this must not be
a silent rewrite bundled into a schema deploy — and NOT a new `/admin/*`
bulk endpoint: a one-off ORM-level backfill script for a pre-existing-row
gap has an established project precedent (see the
email-verification backfill note in `docs/mechanisms/email-verification.md`,
issue #260). Idempotent: re-running is a no-op for every row already
correctly resolved. Every change prints the decrypted ticker/name (PR #314
review: the log previously showed only the DB id/UUIDs, which are useless
for an operator to sanity-check a promotion before `--apply` without a
second decrypting session) plus old/new market, ticker, and
capture_supported — nothing here is silent.

Run once, after deploying #313:

    python -m app.scripts.backfill_capture_supported          # dry run
    python -m app.scripts.backfill_capture_supported --apply  # commit changes
"""

from __future__ import annotations

import sys
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.holding import Holding
from app.services.holding_parser import (
    apply_confirmed_exchange_suffix,
    normalize_ticker_and_currency,
)
from app.services.markets import resolve_holding_market


def _resolve(h: Holding) -> tuple[str | None, str | None, bool]:
    """Confirm-parity resolution for one holding; returns (market, ticker, capture_supported).

    `data["market"]` is seeded `None`, NOT `h.market` ("Other") — this is
    the load-bearing detail. `apply_confirmed_exchange_suffix`'s own gate
    (`_confirmed_market`) treats any value already in `VALID_HOLDING_MARKETS`
    — "Other" included — as an already-confirmed market and returns
    immediately, short-circuiting BEFORE it ever reaches the currency-based
    inference (GBP -> UK, etc.) that would otherwise force the suffix. Since
    every row here already has `market="Other"` stored, passing it straight
    through would silently no-op the whole suffix-forcing step. Re-resolving
    an existing Other row means treating "Other" as *undetermined*, not as
    an already-confirmed value — the final `resolve_holding_market(
    declared_market=...)` call below is passed `data["market"]` (the value
    AFTER suffix-forcing has run, NOT the original `h.market`), the same
    call-argument source `_apply_write_defaults` uses. The *seed* is not
    the same — `_apply_write_defaults` seeds the stored `"Other"`,
    this function seeds `None` — which is why the two produce different
    outcomes for a still-bare, currency-hinted ticker even though both
    call `resolve_holding_market` the same way; `resolve_holding_market`'s
    own "declared Other does not win over a resolvable ticker" rule then
    handles the resolved value correctly either way.
    """
    data: dict[str, Any] = {
        "ticker": h.ticker,
        "market": None,
        "fund_code": h.fund_code,
        "asset_type": h.asset_type,
        "pricing_mode": h.pricing_mode,
        "currency": h.currency,
    }
    # Same 3-call sequence as _apply_write_defaults: suffix-force BEFORE
    # resolving market, so a still-bare ticker with a currency hint cannot
    # fall through resolve_holding_market's bare-ticker "no suffix = US"
    # default.
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
    if data.pop("_suffix_ambiguous", False):
        capture_ok = False
    ticker_value = data.get("ticker")
    return resolved_market, (str(ticker_value) if ticker_value is not None else None), capture_ok


def backfill_capture_supported(session: Session, *, apply_changes: bool) -> int:
    """Re-resolve every `market="Other"` holding except cash/wmf; return the
    count changed.

    `asset_type in ("cash", "wmf")` is skipped in the scan itself, not just
    a no-op inside `_resolve` (issue #316 item 1) — those rows are never in
    scope for this rewrite regardless of what ticker they carry. Mutates
    ORM rows in place when `apply_changes` (caller commits) — a dry run
    only prints what would change, matching `apply_changes=False`'s
    contract of leaving `session` untouched.
    """
    # Cash/WMP rows are never in scope for this rewrite (issue #313 item 4 is
    # about listed-Other tickers) and skipping them here is a safety net, not
    # just an optimization: `resolve_holding_market` already treats a normal
    # no-ticker cash/wmf row as a no-op, but a row that predates issue #120
    # can still carry a spurious ticker string (e.g. "CASH") — that would hit
    # `market_from_ticker`'s bare-ticker branch and get promoted to
    # market="US" before ever reaching resolve_holding_market's asset_type
    # check (PR #314 review round 2).
    rows = (
        session.query(Holding)
        .filter(Holding.market == "Other")
        .filter(or_(Holding.asset_type.is_(None), Holding.asset_type.notin_(("cash", "wmf"))))
        .all()
    )
    changed = 0
    for h in rows:
        resolved_market, resolved_ticker, capture_ok = _resolve(h)
        if (
            resolved_market == h.market
            and resolved_ticker == h.ticker
            and capture_ok == h.capture_supported
        ):
            continue
        changed += 1
        tag = "APPLY" if apply_changes else "DRY-RUN"
        print(
            f"[{tag}] holding {h.id} (user {h.user_id}) "
            f"name={h.name!r} ticker={h.ticker!r}: "
            f"market {h.market!r} -> {resolved_market!r}, "
            f"ticker {h.ticker!r} -> {resolved_ticker!r}, "
            f"capture_supported {h.capture_supported} -> {capture_ok}"
        )
        if apply_changes:
            h.market = resolved_market
            h.ticker = resolved_ticker
            h.capture_supported = capture_ok
    verb = "updated" if apply_changes else "would change"
    print(f"[OK] {changed} row(s) {verb} out of {len(rows)} market='Other' holding(s) scanned")
    return changed


def main() -> None:
    apply_changes = "--apply" in sys.argv
    with SessionLocal() as session:
        changed = backfill_capture_supported(session, apply_changes=apply_changes)
        if apply_changes and changed:
            session.commit()


if __name__ == "__main__":
    main()
