"""encrypt holdings identity/amount columns at rest

issue #31 (Ring 0 audit P1-7, deferred): holdings data was stored plaintext.
Encrypts the columns that identify what a user holds and how much — name,
ticker, fund_code, shares, avg_cost, current_value, market_price, broker,
account, portfolio, notes — using Fernet (app/core/encryption.py). Not
encrypted: asset_type/asset_class/sector/market/currency/pricing_mode/
position (classification buckets, not individually identifying; kept
queryable for the SQL-level NULL/equality filters elsewhere in the codebase —
see EncryptedString's docstring for the specific call sites).

Two different techniques depending on the pre-existing column type:
- Already-Text columns (name, ticker, fund_code, broker, account, portfolio,
  notes): encrypted in place, one UPDATE per row, no type change.
- Numeric columns (shares, avg_cost, current_value, market_price): Fernet
  tokens aren't valid numeric literals, so these go through an add-populate-
  drop-rename sequence to land as Text.

Requires HOLDINGS_ENCRYPTION_KEY to be set wherever this migration runs
(dev/.env.local, prod/.env) — see app/core/config.py.

Revision ID: 379fdb627ee8
Revises: e3ba6849cb56
Create Date: 2026-08-09
"""

from decimal import Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.encryption import decrypt_value, encrypt_value

revision: str = "379fdb627ee8"
down_revision: Union[str, Sequence[str], None] = "e3ba6849cb56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TEXT_COLUMNS = ("name", "ticker", "fund_code", "broker", "account", "portfolio", "notes")
_NUMERIC_COLUMNS = ("shares", "avg_cost", "current_value", "market_price")


def upgrade() -> None:
    conn = op.get_bind()

    for col in _TEXT_COLUMNS:
        rows = conn.execute(
            sa.text(f"SELECT id, {col} FROM holdings WHERE {col} IS NOT NULL")  # noqa: S608
        ).fetchall()
        for row_id, value in rows:
            conn.execute(
                sa.text(f"UPDATE holdings SET {col} = :val WHERE id = :id"),  # noqa: S608
                {"val": encrypt_value(value), "id": row_id},
            )

    for col in _NUMERIC_COLUMNS:
        enc_col = f"{col}_enc"
        op.add_column("holdings", sa.Column(enc_col, sa.Text(), nullable=True))
        rows = conn.execute(
            sa.text(f"SELECT id, {col} FROM holdings WHERE {col} IS NOT NULL")  # noqa: S608
        ).fetchall()
        for row_id, value in rows:
            conn.execute(
                sa.text(f"UPDATE holdings SET {enc_col} = :val WHERE id = :id"),  # noqa: S608
                {"val": encrypt_value(str(value)), "id": row_id},
            )
        op.drop_column("holdings", col)
        op.alter_column("holdings", enc_col, new_column_name=col)


def downgrade() -> None:
    conn = op.get_bind()

    for col in _TEXT_COLUMNS:
        rows = conn.execute(
            sa.text(f"SELECT id, {col} FROM holdings WHERE {col} IS NOT NULL")  # noqa: S608
        ).fetchall()
        for row_id, value in rows:
            conn.execute(
                sa.text(f"UPDATE holdings SET {col} = :val WHERE id = :id"),  # noqa: S608
                {"val": decrypt_value(value), "id": row_id},
            )

    for col in _NUMERIC_COLUMNS:
        plain_col = f"{col}_plain"
        op.add_column("holdings", sa.Column(plain_col, sa.Numeric(), nullable=True))
        rows = conn.execute(
            sa.text(f"SELECT id, {col} FROM holdings WHERE {col} IS NOT NULL")  # noqa: S608
        ).fetchall()
        for row_id, value in rows:
            conn.execute(
                sa.text(f"UPDATE holdings SET {plain_col} = :val WHERE id = :id"),  # noqa: S608
                {"val": Decimal(decrypt_value(value)), "id": row_id},
            )
        op.drop_column("holdings", col)
        op.alter_column("holdings", plain_col, new_column_name=col)
