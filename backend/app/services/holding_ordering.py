"""Shared holdings book-order sort key.

Extracted from app/routers/holdings.py's `_sorted_holdings` (issue #92) so
app/services/portfolio_calculator.py can use the same order without a
service importing a router (issue #320 review round 3, PR #322).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models.holding import Holding


def sorted_holdings(rows: Sequence[Holding]) -> list[Holding]:
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
