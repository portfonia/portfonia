"""Encrypt holdings-derived columns at rest (#791).

Revision ID: d79100000001
Revises: d78300000001
"""

import json
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op
from app.core.encryption import decrypt_value, encrypt_value

revision = "d79100000001"
down_revision = "d78300000001"
branch_labels = None
depends_on = None

_TEXT_COLUMNS = (
    ("reports", "report_md"),
    ("reports", "report_html"),
    ("upload_jobs", "raw_text"),
    ("macro_coverage", "paragraph_text"),
)
_NUMERIC_COLUMNS = (
    ("portfolio_value_snapshots", "market_value"),
    ("portfolio_value_snapshots", "market_value_base"),
    ("portfolio_value_snapshots", "cost_basis_base"),
)
_JSON_COLUMNS = (("reports", "report_inputs"), ("upload_jobs", "preview"))


def upgrade() -> None:
    conn = op.get_bind()
    for table, col in _TEXT_COLUMNS:
        rows = conn.execute(
            sa.text(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL")  # noqa: S608
        ).fetchall()
        for row_id, value in rows:
            conn.execute(
                sa.text(f"UPDATE {table} SET {col} = :val WHERE id = :id"),  # noqa: S608
                {"val": encrypt_value(value), "id": row_id},
            )

    for table, col in _NUMERIC_COLUMNS + _JSON_COLUMNS:
        enc_col = f"{col}_enc"
        op.add_column(table, sa.Column(enc_col, sa.Text(), nullable=True))
        rows = conn.execute(
            sa.text(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL")  # noqa: S608
        ).fetchall()
        for row_id, value in rows:
            plaintext = (
                json.dumps(value, ensure_ascii=False) if (table, col) in _JSON_COLUMNS else str(value)
            )
            conn.execute(
                sa.text(f"UPDATE {table} SET {enc_col} = :val WHERE id = :id"),  # noqa: S608
                {"val": encrypt_value(plaintext), "id": row_id},
            )
        op.drop_column(table, col)
        op.alter_column(table, enc_col, new_column_name=col)

    op.add_column("macro_coverage", sa.Column("affected_identifiers_enc", sa.Text(), nullable=True))
    rows = conn.execute(sa.text("SELECT id, affected_identifiers FROM macro_coverage")).fetchall()
    for row_id, value in rows:
        conn.execute(
            sa.text("UPDATE macro_coverage SET affected_identifiers_enc = :val WHERE id = :id"),
            {"val": encrypt_value(json.dumps(list(value))), "id": row_id},
        )
    op.alter_column("macro_coverage", "affected_identifiers_enc", nullable=False)
    op.drop_column("macro_coverage", "affected_identifiers")
    op.alter_column("macro_coverage", "affected_identifiers_enc", new_column_name="affected_identifiers")


def downgrade() -> None:
    conn = op.get_bind()
    for table, col in _TEXT_COLUMNS:
        rows = conn.execute(
            sa.text(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL")  # noqa: S608
        ).fetchall()
        for row_id, value in rows:
            conn.execute(
                sa.text(f"UPDATE {table} SET {col} = :val WHERE id = :id"),  # noqa: S608
                {"val": decrypt_value(value), "id": row_id},
            )

    for table, col in _NUMERIC_COLUMNS + _JSON_COLUMNS:
        plain_col = f"{col}_plain"
        is_json = (table, col) in _JSON_COLUMNS
        op.add_column(table, sa.Column(plain_col, JSONB() if is_json else sa.Numeric(), nullable=True))
        rows = conn.execute(
            sa.text(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL")  # noqa: S608
        ).fetchall()
        for row_id, value in rows:
            statement = sa.text(f"UPDATE {table} SET {plain_col} = :val WHERE id = :id")  # noqa: S608
            if is_json:
                statement = statement.bindparams(sa.bindparam("val", type_=JSONB))
            conn.execute(
                statement,
                {"val": json.loads(decrypt_value(value)) if is_json else Decimal(decrypt_value(value)),
                 "id": row_id},
            )
        op.drop_column(table, col)
        op.alter_column(table, plain_col, new_column_name=col)

    op.add_column("macro_coverage", sa.Column(
        "affected_identifiers_plain", sa.ARRAY(sa.Text()), nullable=False,
        server_default=sa.text("'{}'"),
    ))
    rows = conn.execute(sa.text("SELECT id, affected_identifiers FROM macro_coverage")).fetchall()
    for row_id, value in rows:
        conn.execute(
            sa.text("UPDATE macro_coverage SET affected_identifiers_plain = :val WHERE id = :id")
            .bindparams(sa.bindparam("val", type_=sa.ARRAY(sa.Text()))),
            {"val": json.loads(decrypt_value(value)), "id": row_id},
        )
    op.drop_column("macro_coverage", "affected_identifiers")
    op.alter_column("macro_coverage", "affected_identifiers_plain", new_column_name="affected_identifiers")
