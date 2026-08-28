"""add accounts table + holdings.account_id; add user_id FKs to holdings/
reports/upload_jobs/news_surfaced

Issue #129 Ring 1 stage B, checkpoint B7 (Ring 1-B design.md §9). Two
independent pieces, bundled in one migration because both are pure schema/
data-normalization work with no user-visible behavior change:

1. Normalizes broker/account/portfolio — free-text, encrypted columns on
   `holdings`, in use since Ring 0 — into an `accounts` table +
   `holdings.account_id`. Decision point 5 (design §9.2/§12.1: "规范化，建
   accounts 表"). The original `broker`/`account`/`portfolio` text columns
   on `holdings` are NOT dropped or cleared: report §1's broker grouping
   (`Custodian`) and the upload parser (`holding_parser.py`) both read those
   three columns directly today, and switching both read paths in the same
   migration would double this migration's failure surface for no
   immediate benefit. `account_id` exists to give stage C's upcoming inline
   entry form a stable id to reference, not to replace the current render
   path. Currency is deliberately NOT promoted onto `accounts` (§2.4): the
   2026-05 spec's "account = 本位币" assumption doesn't match reality — a
   single broker/account routinely holds more than one currency (e.g. IBKR:
   USD equities + HKD equities in the same account; the upload preview's
   `BrokerGroup.subtotals: CurrencySubtotal[]` already assumes this).

   Backfill groups each user's existing holdings by DECRYPTED (broker,
   account, portfolio) plaintext tuple, not by the ciphertext columns
   directly — Fernet's random IV means two encryptions of the identical
   plaintext never produce the same ciphertext (verified against production
   2026-08-28: 26/27 holding rows per user, 26/27 distinct broker
   ciphertexts each, even where the plaintext repeats). A holding with a
   NULL `broker` gets no `accounts` row and keeps `account_id` NULL —
   `accounts.broker` is NOT NULL, and report §1 already buckets broker-less
   holdings into "Other" (CLAUDE.md holdings model row), so there is no
   real institution for such a holding to normalize against.

2. Adds `FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT` to
   `holdings`, `reports`, `upload_jobs`, `news_surfaced` — closing the gap
   design §2.2's multi-user audit found: none of these four tables had a
   real FK (`user_investment_context` was the only user-scoped table with
   one, because it postdates B4 and never had legacy pre-FK data).
   RESTRICT, not CASCADE (design §9.3, explicit decision): a bare
   `DELETE FROM users` must never silently cascade into a user's holdings
   or report history. Deletion is `app/services/user_purge.py`'s explicit,
   ordered, audited function (issue #199, extended by #225 for the
   Supabase Auth side) — accounts are deleted there too, after holdings and
   before users, in the same commit.

`holdings.account_id` carries a COMPOSITE FK `(account_id, user_id) ->
accounts (id, user_id)`, not a single-column FK on `account_id` alone
(review, PR #247): a single-column FK only guarantees the account exists,
not that it belongs to the same user as the holding — once any writer
other than this migration's own per-user backfill sets `account_id`
(confirm, stage C, an admin script), a single-column FK would let a
holding point at another user's account. `accounts` gets a
`UNIQUE (id, user_id)` constraint so Postgres has something to reference
for the pair (`id` alone is already unique via the PK).

Pre-migration safety check (not re-run by this migration itself): production
audited 2026-08-28 — 4 users, 0 orphan `user_id` rows across all four
tables. Re-verify this still holds immediately before running against
production (design §9.4 acceptance checklist) — this migration assumes it,
it does not enforce it.

Revision ID: 4edf69bf41ab
Revises: b1c2d3e4f5a6
Create Date: 2026-08-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.encryption import decrypt_value, encrypt_value

revision: str = "4edf69bf41ab"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalize(value: str | None) -> str | None:
    """Blank/whitespace-only collapses to None (review, PR #247 round 2) —
    matches app/services/accounts.py's normalization, duplicated here
    rather than imported: a migration must stay a frozen historical
    snapshot, immune to that module changing later (same rationale as this
    migration's own encrypt_value/decrypt_value imports, which are stable
    crypto primitives, not business logic that could legitimately drift)."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _backfill_accounts(conn: sa.engine.Connection) -> None:
    rows = conn.execute(
        sa.text(
            "SELECT id, user_id, broker, account, portfolio FROM holdings "
            "WHERE broker IS NOT NULL"
        )
    ).fetchall()

    # (user_id, broker_plain, account_plain, portfolio_plain) -> accounts.id
    seen: dict[tuple[object, str, str | None, str | None], str] = {}
    for holding_id, user_id, broker_enc, account_enc, portfolio_enc in rows:
        broker_plain = _normalize(decrypt_value(broker_enc))
        if broker_plain is None:
            # Blank/whitespace-only broker — same as no broker at all
            # (holding_parser._summarize / report_sections.py both already
            # bucket it as "Other"). account_id stays NULL on this row.
            continue
        account_plain = (
            _normalize(decrypt_value(account_enc)) if account_enc is not None else None
        )
        portfolio_plain = (
            _normalize(decrypt_value(portfolio_enc)) if portfolio_enc is not None else None
        )
        key = (user_id, broker_plain, account_plain, portfolio_plain)
        account_id = seen.get(key)
        if account_id is None:
            result = conn.execute(
                sa.text(
                    "INSERT INTO accounts (user_id, broker, account, portfolio) "
                    "VALUES (:user_id, :broker, :account, :portfolio) RETURNING id"
                ),
                {
                    "user_id": user_id,
                    "broker": encrypt_value(broker_plain),
                    "account": encrypt_value(account_plain) if account_plain is not None else None,
                    "portfolio": (
                        encrypt_value(portfolio_plain) if portfolio_plain is not None else None
                    ),
                },
            )
            account_id = result.scalar_one()
            seen[key] = account_id
        conn.execute(
            sa.text("UPDATE holdings SET account_id = :account_id WHERE id = :id"),
            {"account_id": account_id, "id": holding_id},
        )


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account", sa.Text(), nullable=True),
        sa.Column("portfolio", sa.Text(), nullable=True),
        sa.Column("archived_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_accounts_user_id_users", "accounts", "users", ["user_id"], ["id"], ondelete="RESTRICT"
    )
    # Lets holdings.account_id carry a composite FK below, so a holding
    # cannot point at another user's account (review, PR #247) — a
    # single-column FK on account_id alone only guarantees the account
    # exists, not that it's this holding's own user's account. `id` alone
    # is already unique (PK); this exists purely to give Postgres a unique
    # target covering the pair.
    op.create_unique_constraint("uq_accounts_id_user_id", "accounts", ["id", "user_id"])

    op.add_column("holdings", sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True))

    conn = op.get_bind()
    _backfill_accounts(conn)

    op.create_foreign_key(
        "fk_holdings_account_id_user_id_accounts",
        "holdings",
        "accounts",
        ["account_id", "user_id"],
        ["id", "user_id"],
        ondelete="RESTRICT",
    )

    for table in ("holdings", "reports", "upload_jobs", "news_surfaced"):
        op.create_foreign_key(
            f"fk_{table}_user_id_users", table, "users", ["user_id"], ["id"], ondelete="RESTRICT"
        )


def downgrade() -> None:
    for table in ("holdings", "reports", "upload_jobs", "news_surfaced"):
        op.drop_constraint(f"fk_{table}_user_id_users", table, type_="foreignkey")

    op.drop_constraint("fk_holdings_account_id_user_id_accounts", "holdings", type_="foreignkey")
    op.drop_column("holdings", "account_id")

    op.drop_constraint("fk_accounts_user_id_users", "accounts", type_="foreignkey")
    op.drop_constraint("uq_accounts_id_user_id", "accounts", type_="unique")
    op.drop_table("accounts")
