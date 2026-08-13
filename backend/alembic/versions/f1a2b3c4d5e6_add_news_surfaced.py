"""add news_surfaced table

Issue #30 (H-DEBT-3): `load_news_window` selected by
`published_at > start, <= end` — a news item published inside a window but
not ingested until after that window's period_end fell through BOTH the
window it belongs to (not yet ingested when selected) and the next one
(excluded as "before this window's start"), a permanent miss. Window
selection now decouples from the watermark boundary entirely
(`published_at <= end`, no lower bound); this table is the dedup ledger that
takes over the job the lower bound used to do — a news item is excluded from
every future window, FOR THAT USER, once it has appeared in a report of
theirs that reached success/needs_review/skipped. Uniqueness is
(user_id, news_id): `news` is a global capture-layer store, but reports are
per-user with independent watermarks, so the same news item can legitimately
need to surface once for each user (PR #139 review).

PR #139 review also flagged that this migration would otherwise be an
EMPTY-LEDGER deploy: with no lower bound and an empty `news_surfaced`, the
first production report generated after this deploys would select the
entire historical `news` table (up to 1yr retention) as "unsurfaced",
poisoning macro-signal detection and quiet-day classification, then mark
all of it surfaced — including items a user was never actually shown. The
backfill below reconstructs history from each DONE report's
`report_inputs['news_items']` (the durable record of what that report
actually included, per report_generator.py's `_serialize_news`), hashing
each item's stored `url` with the same `_url_hash` (news_fetcher.py:
MD5-16 of the URL) to resolve it back to a `news.id`. This is why the
backfill can't be "mark everything with published_at <= max(period_end) as
surfaced" — that would blanket-mark late-ingested stragglers that were
NEVER shown to anyone, reintroducing H-DEBT-3 by a different mechanism.

Revision ID: f1a2b3c4d5e6
Revises: ed3e81d6cccb
Create Date: 2026-08-13 00:00:00.000000

"""

import hashlib
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "ed3e81d6cccb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Snapshot of news_fetcher._url_hash at the time of this migration — frozen
# here deliberately (not imported from app code), matching this repo's
# established migration convention of not live-importing app logic that
# could drift after this migration is merged.
_DONE_STATUSES = ("success", "needs_review", "skipped")


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:16]


def upgrade() -> None:
    op.create_table(
        "news_surfaced",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("news_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "surfaced_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Unique on (user_id, news_id) — see module docstring: news is a global
    # store but reports are per-user, so the same item can legitimately need
    # to surface once per user. This is also the ON CONFLICT DO NOTHING
    # target for idempotent marking against Celery redelivery
    # (task_acks_late).
    op.create_unique_constraint(
        "uq_news_surfaced_user_news", "news_surfaced", ["user_id", "news_id"]
    )
    op.create_index("ix_news_surfaced_report_id", "news_surfaced", ["report_id"])

    _backfill_from_report_history()


def _backfill_from_report_history() -> None:
    """Reconstruct news_surfaced from every DONE report's stored news_items,
    so this deploy doesn't dump a year of history into the next report run.
    """
    bind = op.get_bind()

    news_t = sa.table("news", sa.column("id"), sa.column("url_hash"))
    reports_t = sa.table(
        "reports",
        sa.column("id"),
        sa.column("user_id"),
        sa.column("status"),
        sa.column("report_inputs"),
    )
    surfaced_t = sa.table(
        "news_surfaced",
        sa.column("user_id"),
        sa.column("news_id"),
        sa.column("report_id"),
    )

    url_hash_to_news_id = {
        row.url_hash: row.id for row in bind.execute(sa.select(news_t.c.id, news_t.c.url_hash))
    }
    if not url_hash_to_news_id:
        return

    rows = bind.execute(
        sa.select(reports_t.c.id, reports_t.c.user_id, reports_t.c.report_inputs).where(
            reports_t.c.status.in_(_DONE_STATUSES),
            reports_t.c.report_inputs.is_not(None),
        )
    ).fetchall()

    to_insert: list[dict[str, object]] = []
    for report_id, user_id, report_inputs in rows:
        for item in (report_inputs or {}).get("news_items", []):
            url = item.get("url")
            if not url:
                continue
            news_id = url_hash_to_news_id.get(_url_hash(url))
            if news_id is None:
                continue
            to_insert.append({"user_id": user_id, "news_id": news_id, "report_id": report_id})

    if not to_insert:
        return

    stmt = (
        postgresql.insert(surfaced_t)
        .values(to_insert)
        .on_conflict_do_nothing(constraint="uq_news_surfaced_user_news")
    )
    bind.execute(stmt)


def downgrade() -> None:
    op.drop_index("ix_news_surfaced_report_id", table_name="news_surfaced")
    op.drop_constraint("uq_news_surfaced_user_news", "news_surfaced", type_="unique")
    op.drop_table("news_surfaced")
