"""Spend and host-resource observability (#886).
Revision ID: k88600000001
Revises: k88000000001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = "k88600000001"
down_revision = "k88000000001"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("llm_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("trigger_source", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12,6), nullable=True),
        sa.CheckConstraint("purpose IN ('report_pass2','report_regenerate','translation','holdings_parse','intel_classifier','intel_name_check')", name=op.f("ck_llm_usage_purpose")),
        sa.CheckConstraint("trigger_source IN ('user','ops','scheduled')", name=op.f("ck_llm_usage_trigger_source")),
    )
    op.create_index("ix_llm_usage_occurred_at", "llm_usage", ["occurred_at"])
    op.create_table("external_call_daily",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("provider", sa.Text(), primary_key=True),
        sa.Column("calls", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("provider IN ('resend','finnhub','massive','twelvedata','yfinance','eastmoney','sina','tencent','google_news','sec','rss','fred')", name=op.f("ck_external_call_daily_provider")),
    )
    op.create_table("host_samples",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("load_15m", sa.Double(), nullable=False),
        sa.Column("mem_total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mem_available_bytes", sa.BigInteger(), nullable=False),
        sa.Column("swap_total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("swap_free_bytes", sa.BigInteger(), nullable=False),
        sa.Column("pswpin", sa.BigInteger(), nullable=False),
        sa.Column("pswpout", sa.BigInteger(), nullable=False),
        sa.Column("oom_kill", sa.BigInteger(), nullable=False),
        sa.Column("disk_used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("disk_avail_bytes", sa.BigInteger(), nullable=False),
        sa.Column("pg_bytes", sa.BigInteger(), nullable=False),
        sa.Column("swappiness", sa.SmallInteger(), nullable=False),
        sa.Column("redis_used_memory_bytes", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_host_samples_sampled_at", "host_samples", ["sampled_at"])

def downgrade() -> None:
    op.drop_table("host_samples")
    op.drop_table("external_call_daily")
    op.drop_table("llm_usage")
