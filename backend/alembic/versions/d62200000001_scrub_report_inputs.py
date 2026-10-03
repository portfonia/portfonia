"""Remove stored URL and source metadata from historical report inputs.

This migration is irreversible. It may only run in an owner-authorized
deployment after that day's database backup.
"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import sqlalchemy as sa

from alembic import op

revision = "d62200000001"
down_revision = "d62100000001"
branch_labels = None
depends_on = None


def _strip_entries(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return [
        {key: item for key, item in entry.items() if key not in {"url", "source"}}
        if isinstance(entry, dict)
        else entry
        for entry in value
    ]


def _scrub(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = deepcopy(value)
    for key in ("news_items", "search_results"):
        result[key] = _strip_entries(result.get(key))
    holding_news = result.get("holding_news")
    if isinstance(holding_news, dict):
        result["holding_news"] = {
            name: _strip_entries(entries) for name, entries in holding_news.items()
        }
    macro_signals = result.get("macro_signals")
    if isinstance(macro_signals, dict):
        for hit in macro_signals.get("hits", []):
            if isinstance(hit, dict):
                hit["top_articles"] = _strip_entries(hit.get("top_articles"))
    return result


def _rows(bind: sa.Connection) -> Iterator[tuple[Any, Any]]:
    table = sa.table(
        "reports",
        sa.column("id"),
        sa.column("report_inputs", sa.JSON),
    )
    last_id: Any = None
    while True:
        stmt = sa.select(table.c.id, table.c.report_inputs).order_by(table.c.id)
        if last_id is not None:
            stmt = stmt.where(table.c.id > last_id)
        stmt = stmt.limit(500)
        batch = list(bind.execute(stmt))
        if not batch:
            return
        for row in batch:
            yield (row.id, row.report_inputs)
        last_id = batch[-1].id


def upgrade() -> None:
    bind = op.get_bind()
    table = sa.table("reports", sa.column("id"), sa.column("report_inputs", sa.JSON))
    for report_id, report_inputs in _rows(bind):
        if isinstance(report_inputs, dict):
            scrubbed = _scrub(report_inputs)
            if scrubbed != report_inputs:
                bind.execute(
                    table.update().where(table.c.id == report_id).values(report_inputs=scrubbed)
                )


def downgrade() -> None:
    # URL/source data cannot be restored.
    pass
