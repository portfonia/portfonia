"""widen benchmark_prices.index_code CHECK for CSI 300

Issue #383: add `csi300` (CSI 300 / yfinance `000300.SS` / CNY) to the
catalog. China A50 is not in this revision — yfinance has no durable
multi-year FTSE China A50 series (see the issue Design comment).

Values below are a FROZEN SNAPSHOT of VALID_BENCHMARK_INDEX_CODES as of
this migration's authoring date — deliberately NOT imported live
(e1f2a3b4c5d6 / a2b3c4d5e6f7 precedent). Widening later is a new
migration.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-09-08

"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: str | Sequence[str] | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen snapshot — see module docstring.
_OLD = ("dow30", "nasdaq", "sp500")
_NEW = ("csi300", "dow30", "nasdaq", "sp500")


def _in_list_sql(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"index_code IN ({quoted})"


def upgrade() -> None:
    # Bare token "index_code" — naming_convention renders
    # ck_benchmark_prices_index_code (6cd7544f63cf gotcha).
    op.drop_constraint("index_code", "benchmark_prices", type_="check")
    op.create_check_constraint("index_code", "benchmark_prices", _in_list_sql(_NEW))


def downgrade() -> None:
    op.drop_constraint("index_code", "benchmark_prices", type_="check")
    op.create_check_constraint("index_code", "benchmark_prices", _in_list_sql(_OLD))
